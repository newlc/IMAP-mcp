"""Tests for Bundle 1-5 improvements:

* Bundle 1: reply quote (trailing blanks), save_draft idempotency,
  auto_archive batching, list_mailboxes pagination + None delimiter,
  CSS sanitizer hardening.
* Bundle 2: extract_recipients_from_thread, thread_summary.
* Bundle 3: export_cache / import_cache with passphrase.
* Bundle 4: SPF/DKIM/DMARC parsing, audit_log + audit_log_query.
* Bundle 5: bm25-weighted FTS5 ranking (subject > body > addresses).
"""

from __future__ import annotations

import asyncio
import email
import json
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from imap_mcp import server as srv
from imap_mcp.cache import EmailCache
from imap_mcp.mail_utils import parse_authentication_results, sanitize_html
from imap_mcp.models import EmailAddress, EmailHeader
from tests.conftest import make_envelope, make_fetch_response


# ===========================================================================
# Bundle 1
# ===========================================================================


REPLY_BODY_TRAILING_BLANKS = (
    b"From: Alice <alice@example.com>\r\n"
    b"To: user@example.com\r\n"
    b"Subject: trailing blanks test\r\n"
    b"Date: Mon, 20 Apr 2026 10:30:00 +0000\r\n"
    b"Message-ID: <orig-trailblank@example.com>\r\n"
    b"Content-Type: text/plain; charset=utf-8\r\n"
    b"\r\n"
    b"line one\r\n"
    b"line two\r\n"
    b"\r\n"
    b"\r\n"
    b"\r\n"
)


class TestReplyQuoteTrailingBlanks:
    def test_reply_strips_trailing_blank_quote_lines(self, imap_client, mock_imap_client):
        mock_imap_client.fetch.side_effect = lambda uids, fields: {
            uids[0]: {b"BODY[]": REPLY_BODY_TRAILING_BLANKS,
                      b"ENVELOPE": make_envelope(),
                      b"FLAGS": (), b"RFC822.SIZE": len(REPLY_BODY_TRAILING_BLANKS)}
        }
        smtp_instance = MagicMock()
        with patch("imap_mcp.imap_client.smtplib.SMTP", return_value=smtp_instance):
            imap_client.reply_email(uid=1, body="My reply.", mailbox="INBOX")
        _, _, raw = smtp_instance.sendmail.call_args[0]
        # Decode the body and confirm no run of bare ">" lines at the end.
        msg = email.message_from_bytes(raw)
        text = ""
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                text = payload.decode("utf-8", errors="replace")
                break
        # The quoted block should end after "> line two", not with "> > > >".
        assert "> line two" in text
        assert "> \n> \n>" not in text


class TestSaveDraftIdempotency:
    @pytest.fixture
    def cache(self, tmp_cache_db):
        return EmailCache(tmp_cache_db, encrypted=False, keyring_username="t-draft")

    def test_save_draft_idempotent_replay(self, imap_client, mock_imap_client, cache):
        imap_client.email_cache = cache
        result1 = imap_client.save_draft(
            to=["a@x.com"], subject="s", body="b",
            idempotency_key="draft-key-1",
        )
        result2 = imap_client.save_draft(
            to=["a@x.com"], subject="s", body="b",
            idempotency_key="draft-key-1",
        )
        assert result1["idempotent_replay"] is False
        assert result2["idempotent_replay"] is True
        # Second call must NOT call append again.
        assert mock_imap_client.append.call_count == 1

    def test_save_draft_no_key_no_idempotency(self, imap_client, mock_imap_client, cache):
        imap_client.email_cache = cache
        imap_client.save_draft(to=["a@x.com"], subject="s", body="b")
        imap_client.save_draft(to=["a@x.com"], subject="s", body="b")
        assert mock_imap_client.append.call_count == 2


class TestAutoArchiveBatching:
    def test_process_auto_archive_batches_large_uid_set(self, imap_client, mock_imap_client):
        from imap_mcp.models import AutoArchiveSender
        # 2500 senders all matching -> single move would otherwise be huge.
        # We mock search() to return 2500 UIDs, all from a matching sender.
        imap_client.auto_archive_senders = [
            AutoArchiveSender(email="spammer@example.com", added_at=datetime.now())
        ]
        uids = list(range(1, 2501))
        mock_imap_client.search.return_value = uids

        def fake_fetch(uids_arg, fields):
            return {
                u: {
                    b"ENVELOPE": make_envelope(
                        from_mailbox="spammer", from_host="example.com"
                    ),
                }
                for u in uids_arg
            }
        mock_imap_client.fetch.side_effect = fake_fetch

        result = imap_client.process_auto_archive(dry_run=False)
        # batch_size = 1000 -> 3 chunks
        assert mock_imap_client.move.call_count == 3
        assert result["archived_count"] == 2500


class TestListMailboxesPagination:
    def test_pagination_returns_cursor(self, imap_client, mock_imap_client):
        # 25 folders
        mock_imap_client.list_folders.return_value = [
            ((b"\\HasNoChildren",), b"/", f"folder-{i}") for i in range(25)
        ]
        result = imap_client.list_mailboxes(limit=10)
        assert result["total"] == 25
        assert len(result["mailboxes"]) == 10
        assert result["next_cursor"] == 10

        # Page 2
        result2 = imap_client.list_mailboxes(cursor=10, limit=10)
        assert len(result2["mailboxes"]) == 10
        assert result2["next_cursor"] == 20

        # Page 3 (last)
        result3 = imap_client.list_mailboxes(cursor=20, limit=10)
        assert len(result3["mailboxes"]) == 5
        assert result3["next_cursor"] is None

    def test_handles_none_delimiter(self, imap_client, mock_imap_client):
        mock_imap_client.list_folders.return_value = [
            ((b"\\HasNoChildren",), None, "INBOX"),
        ]
        result = imap_client.list_mailboxes()
        assert result["mailboxes"][0].delimiter == "/"

    def test_invalid_pagination_rejected(self, imap_client, mock_imap_client):
        with pytest.raises(ValueError):
            imap_client.list_mailboxes(cursor=-1, limit=10)
        with pytest.raises(ValueError):
            imap_client.list_mailboxes(limit=0)


class TestCssSanitizerHardening:
    def test_dangerous_css_props_dropped(self):
        # Whitelist-based sanitizer drops position/clip-path/filter/transform.
        out = sanitize_html(
            '<p style="position:fixed;clip-path:inset(0);filter:url(#x);'
            'transform:scale(99);color:red">x</p>'
        )
        assert "position" not in out
        assert "clip-path" not in out
        assert "filter" not in out
        assert "transform" not in out
        # color is safe and stays.
        assert "color" in out


# ===========================================================================
# Bundle 2 — AI-UX
# ===========================================================================


class TestExtractRecipientsFromThread:
    def test_aggregates_with_role_priority(self, imap_client, mock_imap_client):
        from imap_mcp.models import EmailHeader, EmailAddress

        thread = [
            EmailHeader(
                uid=1, subject="x",
                from_address=EmailAddress(name="Alice", email="alice@x.com"),
                to_addresses=[EmailAddress(email="bob@x.com")],
                cc_addresses=[EmailAddress(email="carol@x.com")],
                date=datetime(2026, 4, 1),
            ),
            EmailHeader(
                uid=2, subject="x",
                from_address=EmailAddress(email="bob@x.com"),
                to_addresses=[EmailAddress(email="alice@x.com")],
                cc_addresses=[EmailAddress(email="carol@x.com")],
                date=datetime(2026, 4, 2),
            ),
        ]
        imap_client.get_thread = lambda uid, mailbox=None: thread

        result = imap_client.extract_recipients_from_thread(uid=1)
        emails = {p["email"]: p for p in result["participants"]}
        assert "alice@x.com" in emails
        assert "bob@x.com" in emails
        assert "carol@x.com" in emails
        # Both alice and bob appear as "from" in some message -> role=from
        assert emails["alice@x.com"]["role"] == "from"
        assert emails["bob@x.com"]["role"] == "from"
        # Carol is only in Cc -> role=cc
        assert emails["carol@x.com"]["role"] == "cc"
        # is_self for the user@example.com fixture address; none here.
        assert all(p["is_self"] is False for p in result["participants"])
        assert result["thread_size"] == 2


class TestThreadSummary:
    def test_summary_collects_metadata(self, imap_client):
        from imap_mcp.models import EmailHeader, EmailAddress
        thread = [
            EmailHeader(
                uid=10, subject="Quarterly review",
                from_address=EmailAddress(email="alice@x.com"),
                to_addresses=[EmailAddress(email="user@example.com")],
                date=datetime(2026, 4, 1, 10, 0),
                flags=["\\Seen"],
            ),
            EmailHeader(
                uid=11, subject="Re: Quarterly review",
                from_address=EmailAddress(email="user@example.com"),
                to_addresses=[EmailAddress(email="alice@x.com")],
                date=datetime(2026, 4, 1, 11, 0),
                flags=["\\Seen"],
            ),
            EmailHeader(
                uid=12, subject="Re: Quarterly review",
                from_address=EmailAddress(email="alice@x.com"),
                to_addresses=[EmailAddress(email="user@example.com")],
                date=datetime(2026, 4, 1, 12, 0),
                flags=[],  # unread
            ),
        ]
        imap_client.get_thread = lambda uid, mailbox=None: thread
        result = imap_client.thread_summary(uid=10)
        assert result["thread_size"] == 3
        assert result["unread_count"] == 1
        assert result["messages_from_self"] == 1
        assert result["span"]["oldest"].startswith("2026-04-01T10")
        assert result["span"]["newest"].startswith("2026-04-01T12")
        # Messages chronological
        assert [m["uid"] for m in result["messages"]] == [10, 11, 12]
        assert result["messages"][1]["from_self"] is True
        assert result["messages"][2]["unread"] is True

    def test_empty_thread(self, imap_client):
        imap_client.get_thread = lambda uid, mailbox=None: []
        result = imap_client.thread_summary(uid=1)
        assert result["thread_size"] == 0


# ===========================================================================
# Bundle 3 — cache portability
# ===========================================================================


class TestPortableCache:
    @pytest.fixture
    def cache_with_data(self, tmp_cache_db):
        cache = EmailCache(tmp_cache_db, encrypted=False, keyring_username="t-port")
        cache.store_email(
            "INBOX", 1,
            {"message_id": "<a@x>", "subject": "Test mail",
             "from_address": {"email": "alice@x.com"}, "to_addresses": [],
             "cc_addresses": [], "date": None, "flags": ["\\Seen"], "size": 100},
            {"text": "Body content", "html": None},
        )
        return cache

    def test_export_then_import_round_trip(self, cache_with_data, tmp_path, tmp_cache_db):
        out = tmp_path / "snapshot.imapmcp"
        export_result = cache_with_data.export_portable("hunter2", str(out))
        assert export_result["exported"] is True
        assert export_result["rows"] == 1
        assert out.stat().st_size > 0

        # Import into a separate empty cache.
        import os
        target_path = str(tmp_path / "target.db")
        target = EmailCache(target_path, encrypted=False, keyring_username="t-tgt")
        assert target.get_cached_count("INBOX") == 0

        import_result = target.import_portable("hunter2", str(out))
        assert import_result["imported"] is True
        assert import_result["rows"] == 1
        # Verify content survived.
        row = target.get_email("INBOX", 1)
        assert row is not None
        assert row["subject"] == "Test mail"
        assert row["body_text"] == "Body content"

    def test_wrong_passphrase_rejected(self, cache_with_data, tmp_path):
        out = tmp_path / "snapshot.imapmcp"
        cache_with_data.export_portable("right", str(out))
        target = EmailCache(str(tmp_path / "tgt.db"), encrypted=False,
                           keyring_username="t-bad")
        with pytest.raises(ValueError, match="Decryption failed"):
            target.import_portable("WRONG", str(out))

    def test_corrupted_file_rejected(self, tmp_cache_db, tmp_path):
        bad = tmp_path / "bad.bin"
        bad.write_bytes(b"this is not an IMAPMCP1 file")
        cache = EmailCache(tmp_cache_db, encrypted=False,
                          keyring_username="t-corr")
        with pytest.raises(ValueError, match="IMAPMCP1"):
            cache.import_portable("anything", str(bad))

    def test_export_requires_passphrase(self, cache_with_data, tmp_path):
        with pytest.raises(ValueError):
            cache_with_data.export_portable("", str(tmp_path / "x.bin"))


# ===========================================================================
# Bundle 4 — SPF/DKIM/DMARC + audit
# ===========================================================================


class TestAuthenticationResults:
    def test_parse_basic(self):
        header = ("mx1.example.com; spf=pass smtp.mailfrom=alice@x.com; "
                  "dkim=pass header.d=x.com; dmarc=pass header.from=x.com")
        result = parse_authentication_results(header)
        assert result["spf"] == "pass"
        assert result["dkim"] == "pass"
        assert result["dmarc"] == "pass"
        assert "raw" in result

    def test_fail_takes_precedence_across_multiple_headers(self):
        h1 = "mx1; spf=pass; dkim=pass"
        h2 = "mx2; spf=fail"
        result = parse_authentication_results([h1, h2])
        assert result["spf"] == "fail"
        assert result["dkim"] == "pass"

    def test_empty_input(self):
        assert parse_authentication_results(None) == {"raw": []}
        assert parse_authentication_results("") == {"raw": []}

    def test_get_email_auth_results_uses_partial_fetch(
        self, imap_client, mock_imap_client
    ):
        captured = {}
        def fake_fetch(uids, fields):
            captured["fields"] = list(fields)
            header = (
                b"Authentication-Results: mx; spf=pass; dkim=fail; dmarc=pass\r\n\r\n"
            )
            return {uids[0]: {b"BODY[HEADER.FIELDS (Authentication-Results)]": header}}
        mock_imap_client.fetch.side_effect = fake_fetch
        result = imap_client.get_email_auth_results(uid=1, mailbox="INBOX")
        assert result["spf"] == "pass"
        assert result["dkim"] == "fail"
        assert result["dmarc"] == "pass"
        assert any("Authentication-Results" in str(f) for f in captured["fields"])


class TestAuditLog:
    @pytest.fixture
    def cache(self, tmp_cache_db):
        return EmailCache(tmp_cache_db, encrypted=False, keyring_username="t-audit")

    def test_record_and_query(self, cache):
        cache.record_audit("work", "send_email", True,
                           {"to": ["a@x.com"], "subject": "Hi"}, "ok", None)
        cache.record_audit("work", "fetch_emails", False,
                           {"limit": 20}, "ok", None)
        cache.record_audit("work", "delete_email", True,
                           {"uids": [1]}, "error", "permission denied")

        rows = cache.query_audit_log(limit=10)
        assert len(rows) == 3
        # Newest first
        assert rows[0]["tool"] == "delete_email"

        write_only = cache.query_audit_log(write_only=True)
        assert all(r["write"] == 1 for r in write_only)
        assert len(write_only) == 2

        by_tool = cache.query_audit_log(tool="send_email")
        assert len(by_tool) == 1
        assert by_tool[0]["status"] == "ok"

    def test_cleanup_audit_log(self, cache):
        # Insert one old, one fresh
        old_ts = (datetime.now() - timedelta(days=120)).isoformat()
        cache.conn.execute(
            "INSERT INTO audit_log (ts, tool, write, status) VALUES (?, ?, ?, ?)",
            (old_ts, "fetch_emails", 0, "ok"),
        )
        cache.record_audit("work", "send_email", True, {}, "ok", None)
        cache.conn.commit()
        result = cache.cleanup_audit_log(older_than_days=90)
        assert result["deleted"] == 1
        assert result["remaining"] == 1


# ===========================================================================
# Bundle 5 — bm25 ranking
# ===========================================================================


class TestBm25Ranking:
    @pytest.fixture
    def populated(self, tmp_cache_db):
        cache = EmailCache(tmp_cache_db, encrypted=False, keyring_username="t-bm25")
        # Email 1: query word in subject only.
        cache.store_email("INBOX", 1, {
            "message_id": "<1>", "subject": "Quarterly invoice for 2026",
            "from_address": {"email": "billing@vendor.com"},
            "to_addresses": [], "cc_addresses": [], "date": None,
            "flags": [], "size": 0,
        }, {"text": "Please pay on time.", "html": None})
        # Email 2: query word in body only.
        cache.store_email("INBOX", 2, {
            "message_id": "<2>", "subject": "Lunch tomorrow",
            "from_address": {"email": "friend@x.com"},
            "to_addresses": [], "cc_addresses": [], "date": None,
            "flags": [], "size": 0,
        }, {"text": "Are you free for invoice discussion?", "html": None})
        # Email 3: query word in from_address only.
        cache.store_email("INBOX", 3, {
            "message_id": "<3>", "subject": "Newsletter",
            "from_address": {"email": "invoice-team@x.com", "name": "Team"},
            "to_addresses": [], "cc_addresses": [], "date": None,
            "flags": [], "size": 0,
        }, {"text": "Various other content here.", "html": None})
        return cache

    def test_subject_ranks_highest(self, populated):
        results = populated.fts_search("invoice")
        assert len(results) == 3
        # bm25 weights subject column at 5.0; subject hit should rank first.
        assert results[0]["uid"] == 1
        # rank column is exposed
        assert "rank" in results[0]
