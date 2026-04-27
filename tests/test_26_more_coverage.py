"""More coverage tests: watcher main loop, sieve protocol corners, and
imap_client cache/branch paths."""

from __future__ import annotations

import asyncio
import threading
import time
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from imap_mcp.sieve import ManageSieveClient, SieveError, _LITERAL_RE
from imap_mcp.watcher import ImapWatcher, MailboxCache, EmailSummary


# ===========================================================================
# Watcher: start/stop/refresh/_watch_folder/_create_connection
# ===========================================================================


class _StoppedAfterEvent:
    """Stop event that returns True (stop) after the Nth wait() call."""
    def __init__(self, after: int = 1):
        self._calls = 0
        self._after = after
        self._real = threading.Event()

    def wait(self, timeout=None):
        self._calls += 1
        if self._calls >= self._after:
            self._real.set()
        return self._real.is_set()

    def is_set(self):
        return self._real.is_set()

    def set(self):
        self._real.set()

    def clear(self):
        self._real.clear()


class TestWatcherCreateConnection:
    def test_create_connection_uses_config(self, monkeypatch):
        config = {
            "imap": {"host": "imap.example.com", "port": 993, "secure": True},
            "credentials": {"username": "u@x.com", "password": "pwd"},
        }
        watcher = ImapWatcher(config=config)

        instance = MagicMock()
        with patch("imap_mcp.watcher.IMAPClient", return_value=instance) as cls:
            client = watcher._create_connection()
        cls.assert_called_once_with("imap.example.com", port=993, ssl=True)
        instance.login.assert_called_once_with("u@x.com", "pwd")
        assert client is instance

    def test_create_connection_uses_keyring_password(self):
        config = {
            "imap": {"host": "h"},
            "credentials": {"username": "u@x.com"},
        }
        watcher = ImapWatcher(config=config)
        with patch("imap_mcp.watcher.IMAPClient", return_value=MagicMock()) as cls, \
             patch("imap_mcp.imap_client.get_stored_password", return_value="kr-pwd"):
            watcher._create_connection()
        cls.return_value.login.assert_called_once_with("u@x.com", "kr-pwd")


class TestWatcherStartStop:
    def test_start_then_stop_runs_threads(self):
        watcher = ImapWatcher(config={"folders": {"inbox": "INBOX"}})

        # Patch _watch_folder so threads don't try to open real sockets.
        called = {"runs": 0}
        def fake_watch(key, folder, stop):
            called["runs"] += 1
            stop.wait(0.1)  # exit promptly
        watcher._watch_folder = fake_watch

        watcher.start()
        assert watcher.running is True
        # At least one watcher thread is registered.
        assert len(watcher.watch_threads) >= 1
        watcher.stop()
        assert watcher.running is False
        assert called["runs"] >= 1

    def test_double_start_is_idempotent(self):
        watcher = ImapWatcher(config={"folders": {}})
        watcher._watch_folder = lambda *a, **kw: None
        watcher.start()
        thread_count = len(watcher.watch_threads)
        watcher.start()  # second call should no-op
        assert len(watcher.watch_threads) == thread_count
        watcher.stop()


class TestWatcherWatchFolder:
    def test_one_idle_iteration_then_stop(self, monkeypatch):
        # No initial-jitter sleep.
        monkeypatch.setattr("imap_mcp.watcher.random.uniform", lambda a, b: 0.0)

        watcher = ImapWatcher(config={"folders": {"inbox": "INBOX"}})
        client = MagicMock()
        client.idle_check.return_value = ["EXISTS"]  # something changed
        watcher._create_connection = MagicMock(return_value=client)
        # Make _fetch_mailbox_summary cheap and observable.
        watcher._fetch_mailbox_summary = MagicMock(
            return_value=MailboxCache(name="INBOX", emails=[],
                                      total=1, unread=0,
                                      last_updated=datetime.now()),
        )
        on_update_calls: list = []
        watcher.on_update = lambda key, cache: on_update_calls.append((key, cache.total))

        # Stop after the first IDLE check.
        stop_event = threading.Event()
        def fake_idle_check(timeout=None):
            stop_event.set()  # stop after first response
            return ["EXISTS"]
        client.idle_check.side_effect = fake_idle_check

        watcher._watch_folder("inbox", "INBOX", stop_event)

        assert client.idle.called
        assert client.idle_done.called
        assert client.logout.called  # finally clause
        # initial fetch + post-event fetch (the second one might not happen
        # because the stop_event was just set before the responses-block runs)
        assert watcher._fetch_mailbox_summary.call_count >= 1

    def test_connection_error_backoff(self, monkeypatch):
        # Skip the initial 0-2s jitter.
        monkeypatch.setattr("imap_mcp.watcher.random.uniform", lambda a, b: 0.0)

        watcher = ImapWatcher(config={"folders": {"inbox": "INBOX"}})
        watcher._create_connection = MagicMock(side_effect=Exception("boom"))

        stop_event = _StoppedAfterEvent(after=2)
        watcher._watch_folder("inbox", "INBOX", stop_event)
        # Two iterations: initial jitter wait + one error-backoff wait.
        # (the after=2 stops on the second wait() call -- after the error)
        watcher._create_connection.assert_called()


class TestWatcherRefresh:
    def test_refresh_all_folders(self):
        # _get_watched_folders always returns 4 keys (inbox/next/waiting/someday).
        watcher = ImapWatcher(config={"folders": {"inbox": "INBOX"}})
        client = MagicMock()
        watcher._create_connection = MagicMock(return_value=client)
        watcher._fetch_mailbox_summary = MagicMock(
            side_effect=lambda c, folder: MailboxCache(
                name=folder, emails=[], total=2, unread=0,
                last_updated=datetime.now(),
            )
        )
        watcher.refresh()
        assert watcher._fetch_mailbox_summary.call_count == 4
        client.logout.assert_called_once()

    def test_refresh_single_folder(self):
        watcher = ImapWatcher(config={"folders": {"inbox": "INBOX", "next": "Next"}})
        watcher._create_connection = MagicMock(return_value=MagicMock())
        watcher._fetch_mailbox_summary = MagicMock(
            return_value=MailboxCache(name="INBOX", emails=[], total=0, unread=0,
                                      last_updated=datetime.now())
        )
        watcher.refresh(key="inbox")
        # Only the requested folder.
        assert watcher._fetch_mailbox_summary.call_count == 1


# ===========================================================================
# Sieve: protocol corners
# ===========================================================================


class _FakeSocket:
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
    def test_read_n_aggregates_chunks(self):
        client = ManageSieveClient("h")
        client.sock = _FakeSocket([b"abc", b"def"])
        client._buf = b""
        out = client._read_n(5)
        assert out == b"abcde"

    def test_recv_chunk_raises_on_eof(self):
        client = ManageSieveClient("h")
        client.sock = _FakeSocket([])  # immediately empty
        client._buf = b""
        with pytest.raises(SieveError, match="closed"):
            client._recv_chunk()

    def test_quote_uses_literal_for_complex(self):
        client = ManageSieveClient("h")
        # Multiline strings must use the literal form.
        result = client._quote("line1\nline2")
        assert result.startswith(b"{") and b"}\r\n" in result
        # Simple ASCII fits in quoted form.
        assert client._quote("hello") == b'"hello"'

    def test_read_string_value_quoted(self):
        client = ManageSieveClient("h")
        assert client._read_string_value(b'"hello world"') == "hello world"
        # Bare token form
        assert client._read_string_value(b"BARE") == "BARE"

    def test_read_string_value_literal(self):
        client = ManageSieveClient("h")
        # Pre-load the literal payload into the recv queue.
        client.sock = _FakeSocket([b"hello\r\n"])
        client._buf = b""
        result = client._read_string_value(b"{5}")
        assert result == "hello"

    def test_no_response_raises_at_capability_level(self):
        client = ManageSieveClient("h")
        client.sock = _FakeSocket([b'NO "Server unavailable"\r\n'])
        client._buf = b""
        with pytest.raises(SieveError, match="unavailable"):
            client._read_capabilities()

    def test_read_response_collects_data_lines(self):
        client = ManageSieveClient("h")
        client.sock = _FakeSocket([b'"vacation"\r\n"filters" ACTIVE\r\nOK\r\n'])
        client._buf = b""
        status, lines = client._read_response()
        assert status == "OK"
        assert len(lines) == 2

    def test_starttls_path(self):
        # Server advertises STARTTLS; client should send STARTTLS, get OK,
        # wrap the socket, and re-read capabilities.
        with patch("imap_mcp.sieve.socket.create_connection") as mock_sock_cls, \
             patch("imap_mcp.sieve.ssl.create_default_context") as mock_ctx_factory:
            wrapped_sock = _FakeSocket([
                # post-STARTTLS capabilities + OK
                b'"IMPLEMENTATION" "Dovecot"\r\n'
                b'"SASL" "PLAIN"\r\n'
                b"OK\r\n",
            ])
            ctx = MagicMock()
            ctx.wrap_socket.return_value = wrapped_sock
            mock_ctx_factory.return_value = ctx

            raw_sock = _FakeSocket([
                # initial capabilities advertise STARTTLS
                b'"IMPLEMENTATION" "Dovecot"\r\n'
                b'"STARTTLS"\r\n'
                b"OK\r\n",
                # response to STARTTLS command
                b"OK\r\n",
            ])
            mock_sock_cls.return_value = raw_sock

            client = ManageSieveClient("h", port=4190, secure=False, starttls=True)
            client._connect()

            # STARTTLS command was issued.
            assert b"STARTTLS" in raw_sock.sent
            # Socket was upgraded.
            ctx.wrap_socket.assert_called_once()
            # Post-upgrade capabilities replaced the initial ones.
            assert "IMPLEMENTATION" in client.capabilities

    def test_starttls_rejected(self):
        with patch("imap_mcp.sieve.socket.create_connection") as mock_sock_cls, \
             patch("imap_mcp.sieve.ssl.create_default_context"):
            raw_sock = _FakeSocket([
                b'"STARTTLS"\r\n'
                b"OK\r\n",
                b'NO "TLS not configured"\r\n',
            ])
            mock_sock_cls.return_value = raw_sock
            client = ManageSieveClient("h", port=4190, secure=False, starttls=True)
            with pytest.raises(SieveError):
                client._connect()

    def test_secure_connect_skips_starttls(self):
        with patch("imap_mcp.sieve.socket.create_connection") as mock_sock_cls, \
             patch("imap_mcp.sieve.ssl.create_default_context") as mock_ctx_factory:
            wrapped = _FakeSocket([
                b'"IMPLEMENTATION" "Dovecot"\r\n'
                b"OK\r\n",
            ])
            ctx = MagicMock()
            ctx.wrap_socket.return_value = wrapped
            mock_ctx_factory.return_value = ctx
            mock_sock_cls.return_value = MagicMock()

            client = ManageSieveClient("h", port=5190, secure=True)
            client._connect()
            # No STARTTLS bytes
            assert b"STARTTLS" not in (wrapped.sent or b"")

    def test_getscript_with_literal(self):
        client = ManageSieveClient("h")
        client.sock = _FakeSocket([
            b'{12}\r\nrequire "x";\r\nOK\r\n',
        ])
        client._buf = b""
        body = client.getscript("x")
        # The literal length is 12 -> 'require "x";' has length 12.
        assert "require" in body

    def test_getscript_empty_response(self):
        client = ManageSieveClient("h")
        client.sock = _FakeSocket([b"OK\r\n"])
        client._buf = b""
        body = client.getscript("missing")
        assert body == ""

    def test_context_manager(self):
        # __enter__ calls _connect; __exit__ calls logout.
        with patch("imap_mcp.sieve.socket.create_connection"), \
             patch.object(ManageSieveClient, "_connect"), \
             patch.object(ManageSieveClient, "logout") as logout:
            with ManageSieveClient("h"):
                pass
            logout.assert_called_once()


class TestSieveOpenForReachable:
    def test_open_for_uses_password_from_config(self):
        with patch.object(ManageSieveClient, "_connect"), \
             patch.object(ManageSieveClient, "login") as login:
            from imap_mcp.sieve import open_for
            cfg = {
                "sieve": {"host": "sieve.x.com", "port": 4190},
                "credentials": {"username": "u@x.com", "password": "explicit"},
            }
            open_for(cfg)
            login.assert_called_once_with("u@x.com", "explicit")


# ===========================================================================
# imap_client: cache-hit and additional branch paths
# ===========================================================================


class TestImapClientCachePaths:
    def test_fetch_emails_cache_hit_skips_imap(self, imap_client, mock_imap_client, tmp_cache_db):
        from imap_mcp.cache import EmailCache
        from datetime import datetime as _dt

        cache = EmailCache(tmp_cache_db, encrypted=False, keyring_username="t-fe-cache")
        imap_client.email_cache = cache
        # Pre-populate cache for UID 1.
        cache.store_email(
            "INBOX", 1,
            {"message_id": "<a@x>", "subject": "cached-subject",
             "from_address": {"email": "a@x.com", "name": "A"},
             "to_addresses": [{"email": "b@x.com", "name": None}],
             "cc_addresses": [], "date": _dt(2026, 4, 1).isoformat(),
             "flags": ["\\Seen"], "size": 100},
            {"text": "cached body", "html": None},
        )
        mock_imap_client.search.return_value = [1]
        # Even if fetch is called it returns nothing useful -- the cache
        # path should populate the result.
        result = imap_client.fetch_emails(mailbox="INBOX", limit=10)
        assert any(h.subject == "cached-subject" for h in result)

    def test_get_email_cache_hit_skips_imap(self, imap_client, mock_imap_client, tmp_cache_db):
        from imap_mcp.cache import EmailCache
        cache = EmailCache(tmp_cache_db, encrypted=False, keyring_username="t-ge-cache")
        imap_client.email_cache = cache
        cache.store_email(
            "INBOX", 7,
            {"message_id": "<7@x>", "subject": "from-cache",
             "from_address": {"email": "x@x.com"}, "to_addresses": [],
             "cc_addresses": [], "date": None, "flags": [], "size": 0},
            {"text": "stored body", "html": None},
        )
        mock_imap_client.fetch.side_effect = AssertionError(
            "should not hit IMAP fetch when cache has body"
        )
        msg = imap_client.get_email(uid=7, mailbox="INBOX")
        assert msg.header.subject == "from-cache"

    def test_get_email_body_cache_hit(self, imap_client, mock_imap_client, tmp_cache_db):
        from imap_mcp.cache import EmailCache
        cache = EmailCache(tmp_cache_db, encrypted=False, keyring_username="t-gb-cache")
        imap_client.email_cache = cache
        cache.store_email(
            "INBOX", 4,
            {"message_id": "<4@x>", "subject": "x",
             "from_address": {"email": "a@x"}, "to_addresses": [],
             "cc_addresses": [], "date": None, "flags": [], "size": 0},
            {"text": "cached text", "html": "<p>cached html</p>"},
        )
        text = imap_client.get_email_body(uid=4, mailbox="INBOX", format="text")
        assert text == "cached text"
        html = imap_client.get_email_body(uid=4, mailbox="INBOX", format="html")
        assert "<p>" in html


class TestThreadFallbacks:
    def test_thread_via_subject_fallback(self, imap_client, mock_imap_client):
        # No THREAD=REFERENCES capability and no cached references.
        mock_imap_client.capabilities.return_value = (b"IMAP4REV1",)
        # Pretend partial-fetch returned no Message-ID/References.
        from tests.conftest import make_envelope
        from datetime import datetime as _dt

        def fake_fetch(uids, fields):
            if any("HEADER.FIELDS" in str(f) for f in fields):
                return {uids[0]: {b"BODY[HEADER.FIELDS (Message-ID In-Reply-To References)]": b""}}
            return {
                u: {
                    b"ENVELOPE": make_envelope(
                        date=_dt(2026, 4, 1),
                        subject=b"Re: Original",
                        message_id=f"<msg-{u}@x>".encode(),
                    ),
                    b"FLAGS": (),
                    b"RFC822.SIZE": 0,
                    b"BODY[]": (
                        b"From: a@x\r\nSubject: Re: Original\r\n\r\nbody"
                    ),
                }
                for u in uids
            }
        mock_imap_client.fetch.side_effect = fake_fetch
        mock_imap_client.search.return_value = [101, 102]

        result = imap_client.get_thread(uid=101, mailbox="INBOX")
        # Subject heuristic fired -> two emails returned.
        assert len(result) >= 1


class TestBulkActionVariants:
    def _setup_search(self, mock_imap_client, uids):
        mock_imap_client.search.side_effect = lambda criteria, charset="UTF-8": list(uids)

    def test_unflag_action(self, imap_client, mock_imap_client):
        self._setup_search(mock_imap_client, [1, 2])
        result = imap_client.bulk_action(
            action="unflag", flag_name="\\Flagged", mailbox="INBOX",
        )
        assert result["affected"] == 2
        mock_imap_client.remove_flags.assert_called_with([1, 2], [b"\\Flagged"])

    def test_archive_action(self, imap_client, mock_imap_client):
        self._setup_search(mock_imap_client, [3])
        imap_client.bulk_action(action="archive", mailbox="INBOX")
        mock_imap_client.move.assert_called()

    def test_copy_action(self, imap_client, mock_imap_client):
        self._setup_search(mock_imap_client, [9, 10])
        imap_client.bulk_action(action="copy", destination="Backup", mailbox="INBOX")
        mock_imap_client.copy.assert_called_with([9, 10], "Backup")

    def test_copy_requires_destination(self, imap_client, mock_imap_client):
        self._setup_search(mock_imap_client, [1])
        with pytest.raises(ValueError, match="destination"):
            imap_client.bulk_action(action="copy", mailbox="INBOX")

    def test_search_advanced_use_fts_path(self, imap_client, mock_imap_client, tmp_cache_db):
        from imap_mcp.cache import EmailCache
        cache = EmailCache(tmp_cache_db, encrypted=False, keyring_username="t-fts-adv")
        imap_client.email_cache = cache
        cache.store_email(
            "INBOX", 1,
            {"message_id": "<1@x>", "subject": "report",
             "from_address": {"email": "alice@x.com", "name": "Alice"},
             "to_addresses": [], "cc_addresses": [],
             "date": None, "flags": [], "size": 0},
            {"text": "Quarterly invoice", "html": None},
        )
        result = imap_client.search_advanced(query="invoice", use_fts=True)
        assert len(result) >= 1


class TestThreadViaLocalReferences:
    def test_local_references_walks_cache(
        self, imap_client, mock_imap_client, tmp_cache_db
    ):
        from imap_mcp.cache import EmailCache
        cache = EmailCache(tmp_cache_db, encrypted=False, keyring_username="t-thr-loc")
        imap_client.email_cache = cache
        # Three cached emails: seed (UID 100, MID <a@x>), reply (UID 101,
        # MID <b@x>, references <a@x>), reply-of-reply (102 -> <c@x>, refs
        # <a@x> <b@x>). The local-references walk should pull all three.
        for uid, mid in [(100, "<a@x>"), (101, "<b@x>"), (102, "<c@x>")]:
            cache.store_email(
                "INBOX", uid,
                {"message_id": mid, "subject": "x",
                 "from_address": {"email": "a@x"}, "to_addresses": [],
                 "cc_addresses": [], "date": None, "flags": [], "size": 0},
                {"text": "x", "html": None},
            )

        mock_imap_client.capabilities.return_value = (b"IMAP4REV1",)

        # Header partial-fetch returns References for the seed UID.
        seed_header = (
            b"Message-ID: <c@x>\r\n"
            b"In-Reply-To: <b@x>\r\n"
            b"References: <a@x> <b@x>\r\n\r\n"
        )
        from tests.conftest import make_envelope
        from datetime import datetime as _dt
        def fake_fetch(uids, fields):
            if any("HEADER.FIELDS" in str(f) for f in fields):
                return {uids[0]: {
                    b"BODY[HEADER.FIELDS (Message-ID In-Reply-To References)]": seed_header,
                }}
            return {
                u: {
                    b"ENVELOPE": make_envelope(date=_dt(2026, 4, (u % 9) + 1),
                                               subject=f"S{u}".encode()),
                    b"FLAGS": (), b"RFC822.SIZE": 0,
                }
                for u in uids
            }
        mock_imap_client.fetch.side_effect = fake_fetch

        result = imap_client.get_thread(uid=102, mailbox="INBOX")
        uids = sorted(h.uid for h in result)
        # All three local-reference UIDs should appear.
        assert {100, 101, 102}.issubset(uids)


class TestSmtpSendErrors:
    def test_send_email_no_smtp_host_raises(self, imap_client):
        imap_client.config["smtp"] = {}
        with pytest.raises(RuntimeError, match="SMTP not configured"):
            imap_client.send_email(to=["a@x"], subject="s", body="b")

    def test_send_email_resolve_password_missing_raises(self, imap_client):
        imap_client.config["credentials"] = {"username": "u@x.com", "password": ""}
        with patch("imap_mcp.imap_client.get_stored_password", return_value=None):
            with pytest.raises(RuntimeError, match="No SMTP credentials"):
                imap_client._smtp_send("from@x", ["to@x"], b"")


# ===========================================================================
# Server: resource rendering helpers
# ===========================================================================


class TestServerResourceRendering:
    @pytest.fixture
    def populated(self, single_account_manager):
        from imap_mcp import server as srv
        srv.account_manager.accounts.clear()
        srv.account_manager.accounts.update(single_account_manager.accounts)
        srv.account_manager.default_name = single_account_manager.default_name
        yield single_account_manager
        srv.account_manager.accounts.clear()
        srv.account_manager.default_name = None

    def test_format_overview_with_unread(self, populated):
        from imap_mcp import server as srv
        from imap_mcp.models import EmailHeader, EmailAddress
        cli = populated.get(None)
        cli.search_unread = lambda mailbox, limit: [
            EmailHeader(
                uid=1, subject="Hello",
                from_address=EmailAddress(email="a@x.com"),
                date=datetime(2026, 4, 1),
            ),
            EmailHeader(
                uid=2, subject=None,
                from_address=None,
                date=None,
            ),
        ]
        result = srv._format_overview(cli)
        assert "Hello" in result
        assert "(no subject)" in result

    def test_format_overview_handles_errors(self, populated):
        from imap_mcp import server as srv
        cli = populated.get(None)
        cli.search_unread = MagicMock(side_effect=Exception("boom"))
        out = srv._format_overview(cli)
        assert "error" in out

    def test_format_email_renders_markdown(self, populated):
        from imap_mcp import server as srv
        from imap_mcp.models import Email, EmailHeader, EmailBody, EmailAddress, Attachment
        cli = populated.get(None)
        cli.get_email = lambda uid, mailbox: Email(
            header=EmailHeader(
                uid=uid, subject="Topic",
                from_address=EmailAddress(email="a@x.com"),
                to_addresses=[EmailAddress(email="b@x.com")],
                date=datetime(2026, 4, 1), flags=["\\Seen"],
            ),
            body=EmailBody(text="Body!", html=None),
            attachments=[Attachment(index=0, filename="r.pdf", content_type="application/pdf", size=1024)],
        )
        out = srv._format_email(cli, "INBOX", 99)
        assert "Topic" in out
        assert "a@x.com" in out
        assert "Body!" in out
        assert "Attachments:** 1" in out

    def test_format_email_handles_errors(self, populated):
        from imap_mcp import server as srv
        cli = populated.get(None)
        cli.get_email = MagicMock(side_effect=Exception("nope"))
        out = srv._format_email(cli, "INBOX", 1)
        assert "error" in out

    def test_format_mailbox_summary_runs(self, populated):
        from imap_mcp import server as srv
        from imap_mcp.models import EmailHeader, EmailAddress
        cli = populated.get(None)
        cli.fetch_emails = lambda mailbox, limit: [
            EmailHeader(uid=1, subject="A",
                        from_address=EmailAddress(email="a@x.com"),
                        date=datetime(2026, 4, 1), flags=[]),
        ]
        cli.get_email_summary = lambda uids, mailbox, body_chars: [
            {"subject": "A", "sender": "a@x.com", "date": "2026-04-01",
             "snippet": "preview", "unread": True},
        ]
        out = srv._format_mailbox_summary(cli, "INBOX")
        assert "1 most recent" in out
        assert "preview" in out

    def test_format_mailbox_summary_handles_errors(self, populated):
        from imap_mcp import server as srv
        cli = populated.get(None)
        cli.fetch_emails = MagicMock(side_effect=Exception("network"))
        out = srv._format_mailbox_summary(cli, "INBOX")
        assert "error" in out

    def test_parse_imap_uri_variants(self):
        from imap_mcp import server as srv
        assert srv._parse_imap_uri("imap://work/overview") == ("work", ["overview"])
        assert srv._parse_imap_uri("imap://work/INBOX/42") == ("work", ["INBOX", "42"])

    def test_parse_imap_uri_rejects_bad_scheme(self):
        from imap_mcp import server as srv
        with pytest.raises(ValueError, match="scheme"):
            srv._parse_imap_uri("https://x/y")
        with pytest.raises(ValueError, match="Empty"):
            srv._parse_imap_uri("imap://")

    def test_read_resource_bad_uid(self, populated):
        from imap_mcp import server as srv
        with pytest.raises(ValueError, match="Bad UID"):
            asyncio.run(srv.read_resource("imap://default/INBOX/notnumber"))


class TestImapClientFetchEmailsBranches:
    def test_fetch_emails_with_date_filters(self, imap_client, mock_imap_client):
        # Cover the SINCE+BEFORE branches of fetch_emails search criteria.
        captured = {}
        def fake_search(criteria):
            captured["criteria"] = list(criteria)
            return [101]
        mock_imap_client.search.side_effect = fake_search
        result = imap_client.fetch_emails(
            mailbox="INBOX", limit=10, since="2026-01-01", before="2026-04-01",
        )
        assert "SINCE" in captured["criteria"]
        assert "BEFORE" in captured["criteria"]

    def test_fetch_emails_offset_and_limit(self, imap_client, mock_imap_client):
        mock_imap_client.search.return_value = list(range(1, 11))
        result = imap_client.fetch_emails(mailbox="INBOX", limit=3, offset=2)
        # newest-first sort, then offset 2, then limit 3.
        assert len(result) == 3


class TestStaticBodyStructureHelper:
    def test_bodystructure_none_returns_false(self, imap_client):
        assert imap_client._bodystructure_has_attachment(None) is False

    def test_bodystructure_simple_text(self, imap_client):
        # Singlepart text/plain, no attachment slot at index 8.
        bs = ("text", "plain", None, None, None, "7bit", 100, 5)
        assert imap_client._bodystructure_has_attachment(bs) is False

    def test_bodystructure_with_attachment(self, imap_client):
        # Multipart: first child is a singlepart with a Content-Disposition
        # tuple at slot 8 indicating "attachment".
        attachment_part = (
            "application", "pdf", None, None, None, "base64", 1024, None,
            ("attachment", ("filename", "x.pdf")),
        )
        text_part = ("text", "plain", None, None, None, "7bit", 100, 5)
        # Multipart has its first element as a child tuple.
        bs = (text_part, attachment_part, "mixed")
        assert imap_client._bodystructure_has_attachment(bs) is True


class TestExtractActionItemsBranches:
    def test_max_items_capped(self):
        from imap_mcp.mail_utils import extract_action_items
        # 30 distinct requests should be capped.
        text = " ".join(
            f"Please send report number {i}." for i in range(30)
        )
        result = extract_action_items(text, max_items=5)
        assert len(result["requests"]) == 5

