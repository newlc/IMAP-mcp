"""Tests for the latest reliability/safety improvements:

* #16 Idempotent send/reply/forward via sent_log
* #17 HTML-only -> plain text fallback in _extract_body
* #18 Partial-fetch (BODY.PEEK[TEXT]<0.N>) in get_email_summary
* #19 limit / batch_size in bulk_action
* #29 Attachment safety: size cap + opt-in path allowlist
"""

from __future__ import annotations

import email
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from imap_mcp.cache import EmailCache
from imap_mcp.mail_utils import html_to_plain
from tests.conftest import make_envelope


# ---------------------------------------------------------------------------
# #17 HTML -> plain text fallback
# ---------------------------------------------------------------------------


HTML_ONLY_BODY = (
    b"MIME-Version: 1.0\r\n"
    b"From: sender@example.com\r\n"
    b"Subject: HTML only\r\n"
    b"Content-Type: text/html; charset=utf-8\r\n"
    b"\r\n"
    b"<p>Hello <strong>world</strong></p>"
    b'<p>Visit <a href="https://example.com/x">our site</a></p>'
)


class TestHtmlPlainFallback:
    def test_html_to_plain_basic(self):
        text = html_to_plain("<p>Hello <strong>world</strong></p>")
        assert "Hello" in text
        assert "world" in text
        assert "<p>" not in text

    def test_html_to_plain_keeps_link_text(self):
        text = html_to_plain('<a href="https://x.com">click</a>')
        assert "click" in text

    def test_extract_body_uses_fallback(self, imap_client):
        msg = email.message_from_bytes(HTML_ONLY_BODY)
        body = imap_client._extract_body(msg)
        assert body.html and "<strong>" in body.html
        # text was synthesized from html
        assert body.text and "world" in body.text
        assert "<strong>" not in body.text


# ---------------------------------------------------------------------------
# #16 Idempotency
# ---------------------------------------------------------------------------


class TestIdempotency:
    @pytest.fixture
    def cache(self, tmp_cache_db):
        return EmailCache(
            tmp_cache_db, encrypted=False, keyring_username="test-idem"
        )

    def test_record_and_lookup(self, cache):
        cache.record_sent("k1", "<msg-1@x>", ["a@x.com"], "Subj", "Sent")
        row = cache.lookup_sent("k1")
        assert row is not None
        assert row["message_id"] == "<msg-1@x>"
        assert row["saved_to_sent"] == "Sent"

    def test_lookup_unknown_key_returns_none(self, cache):
        assert cache.lookup_sent("nope") is None

    def test_send_email_short_circuits_on_known_key(
        self, imap_client, mock_imap_client, cache
    ):
        imap_client.email_cache = cache
        cache.record_sent(
            "agent-batch-001", "<orig-1@x>", ["alice@x.com"], "Hi", "Sent"
        )
        # SMTP must NOT be invoked the second time around.
        with patch("imap_mcp.imap_client.smtplib.SMTP") as smtp_cls:
            result = imap_client.send_email(
                to=["alice@x.com"], subject="Hi", body="b",
                idempotency_key="agent-batch-001",
            )
        smtp_cls.assert_not_called()
        assert result["sent"] is True
        assert result["idempotent_replay"] is True
        assert result["message_id"] == "<orig-1@x>"

    def test_send_email_records_sent_log(
        self, imap_client, mock_imap_client, cache
    ):
        imap_client.email_cache = cache
        smtp_instance = MagicMock()
        with patch("imap_mcp.imap_client.smtplib.SMTP",
                   return_value=smtp_instance):
            result = imap_client.send_email(
                to=["alice@x.com"], subject="Hi", body="b",
                idempotency_key="agent-batch-002",
            )
        smtp_instance.sendmail.assert_called_once()
        assert result["idempotent_replay"] is False
        # Second call with the same key replays without SMTP.
        with patch("imap_mcp.imap_client.smtplib.SMTP") as smtp_cls:
            replay = imap_client.send_email(
                to=["alice@x.com"], subject="Hi", body="b",
                idempotency_key="agent-batch-002",
            )
        smtp_cls.assert_not_called()
        assert replay["idempotent_replay"] is True
        assert replay["message_id"] == result["message_id"]

    def test_no_key_no_lookup(self, imap_client, mock_imap_client, cache):
        imap_client.email_cache = cache
        smtp_instance = MagicMock()
        with patch("imap_mcp.imap_client.smtplib.SMTP",
                   return_value=smtp_instance):
            r1 = imap_client.send_email(to=["a@x.com"], subject="s", body="b")
            r2 = imap_client.send_email(to=["a@x.com"], subject="s", body="b")
        # Two separate sends.
        assert smtp_instance.sendmail.call_count == 2
        assert r1["idempotent_replay"] is False
        assert r2["idempotent_replay"] is False


# ---------------------------------------------------------------------------
# #18 Partial FETCH in get_email_summary
# ---------------------------------------------------------------------------


class TestPartialFetchSummary:
    def test_summary_uses_partial_body_fetch(
        self, imap_client, mock_imap_client
    ):
        captured = {}
        def fake_fetch(uids, fields):
            captured["fields"] = list(fields)
            return {
                u: {
                    b"ENVELOPE": make_envelope(),
                    b"FLAGS": (),
                    b"RFC822.SIZE": 5000,
                    b"BODYSTRUCTURE": None,
                    b"BODY[TEXT]<0>": b"first 200 bytes of body...",
                }
                for u in uids
            }
        mock_imap_client.fetch.side_effect = fake_fetch
        imap_client.get_email_summary(uids=[1], body_chars=50, peek_bytes=200)
        assert any("BODY.PEEK[TEXT]<0.200>" in str(f) for f in captured["fields"])

    def test_peek_bytes_zero_skips_body(self, imap_client, mock_imap_client):
        captured = {}
        def fake_fetch(uids, fields):
            captured["fields"] = list(fields)
            return {
                u: {
                    b"ENVELOPE": make_envelope(),
                    b"FLAGS": (),
                    b"RFC822.SIZE": 100,
                    b"BODYSTRUCTURE": None,
                }
                for u in uids
            }
        mock_imap_client.fetch.side_effect = fake_fetch
        result = imap_client.get_email_summary(uids=[1], peek_bytes=0)
        assert all("BODY" not in str(f) or "BODYSTRUCTURE" in str(f)
                   for f in captured["fields"])
        assert result[0]["snippet"] == ""

    def test_default_peek_scales_with_body_chars(
        self, imap_client, mock_imap_client
    ):
        captured = {}
        def fake_fetch(uids, fields):
            captured["fields"] = list(fields)
            return {u: {b"ENVELOPE": make_envelope(), b"FLAGS": (),
                       b"RFC822.SIZE": 0, b"BODYSTRUCTURE": None,
                       b"BODY[TEXT]<0>": b""} for u in uids}
        mock_imap_client.fetch.side_effect = fake_fetch
        # body_chars=500 -> peek defaults to max(500*4, 1024) = 2000
        imap_client.get_email_summary(uids=[1], body_chars=500)
        assert any("BODY.PEEK[TEXT]<0.2000>" in str(f) for f in captured["fields"])


# ---------------------------------------------------------------------------
# #19 bulk_action limit + batch_size
# ---------------------------------------------------------------------------


class TestBulkLimitBatch:
    def _setup_search(self, mock_imap_client, count):
        uids = list(range(1, count + 1))
        mock_imap_client.search.side_effect = lambda criteria, charset="UTF-8": list(uids)
        return uids

    def test_limit_caps_uids(self, imap_client, mock_imap_client):
        self._setup_search(mock_imap_client, 50)
        result = imap_client.bulk_action(
            action="mark_read", mailbox="INBOX", limit=10,
        )
        assert result["matched"] == 50
        assert result["affected"] == 10
        assert result["truncated"] is True
        # Oldest-first: UIDs 1..10
        assert sorted(mock_imap_client.add_flags.call_args_list[0][0][0]) == list(range(1, 11))

    def test_batch_size_chunks_calls(self, imap_client, mock_imap_client):
        self._setup_search(mock_imap_client, 25)
        imap_client.bulk_action(
            action="mark_read", mailbox="INBOX", batch_size=10,
        )
        # 25 / 10 -> 3 chunks
        assert mock_imap_client.add_flags.call_count == 3

    def test_no_limit_no_truncation(self, imap_client, mock_imap_client):
        self._setup_search(mock_imap_client, 5)
        result = imap_client.bulk_action(action="mark_read", mailbox="INBOX")
        assert result["matched"] == 5
        assert result["affected"] == 5
        assert result["truncated"] is False

    def test_invalid_batch_size_rejected(self, imap_client, mock_imap_client):
        with pytest.raises(ValueError, match="batch_size"):
            imap_client.bulk_action(action="mark_read", batch_size=0)

    def test_invalid_limit_rejected(self, imap_client, mock_imap_client):
        with pytest.raises(ValueError, match="limit"):
            imap_client.bulk_action(action="mark_read", limit=-1)

    def test_dry_run_with_limit(self, imap_client, mock_imap_client):
        self._setup_search(mock_imap_client, 100)
        result = imap_client.bulk_action(
            action="delete", mailbox="INBOX", limit=20, dry_run=True,
        )
        assert result["matched"] == 100
        assert len(result["uids"]) == 20
        assert result["affected"] == 0
        mock_imap_client.move.assert_not_called()


# ---------------------------------------------------------------------------
# #29 Attachment safety
# ---------------------------------------------------------------------------


class TestAttachmentSafety:
    def test_size_limit_enforced(self, imap_client, tmp_path):
        big = tmp_path / "big.bin"
        big.write_bytes(b"x" * (2 * 1024 * 1024))
        imap_client.config["security"] = {"max_attachment_size_mb": 1}
        with patch("imap_mcp.imap_client.smtplib.SMTP", return_value=MagicMock()):
            with pytest.raises(ValueError, match="MB; limit"):
                imap_client.send_email(
                    to=["a@x.com"], subject="s", body="b",
                    attachments=[str(big)],
                )

    def test_size_limit_disabled_with_zero(self, imap_client, tmp_path):
        big = tmp_path / "big.bin"
        big.write_bytes(b"x" * (2 * 1024 * 1024))
        imap_client.config["security"] = {"max_attachment_size_mb": 0}
        with patch("imap_mcp.imap_client.smtplib.SMTP",
                   return_value=MagicMock()):
            imap_client.send_email(
                to=["a@x.com"], subject="s", body="b",
                attachments=[str(big)],
            )

    def test_allowlist_blocks_outside_paths(self, imap_client, tmp_path):
        allowed = tmp_path / "allowed"
        outside = tmp_path / "outside"
        allowed.mkdir()
        outside.mkdir()
        bad = outside / "secret.txt"
        bad.write_text("nope")
        imap_client.config["security"] = {
            "attachments_allowed_dirs": [str(allowed)],
            "max_attachment_size_mb": 25,
        }
        with patch("imap_mcp.imap_client.smtplib.SMTP",
                   return_value=MagicMock()):
            with pytest.raises(PermissionError, match="allowlist"):
                imap_client.send_email(
                    to=["a@x.com"], subject="s", body="b",
                    attachments=[str(bad)],
                )

    def test_allowlist_permits_inside_paths(self, imap_client, tmp_path):
        allowed = tmp_path / "allowed"
        allowed.mkdir()
        good = allowed / "report.pdf"
        good.write_bytes(b"%PDF-1.4")
        imap_client.config["security"] = {
            "attachments_allowed_dirs": [str(allowed)],
        }
        with patch("imap_mcp.imap_client.smtplib.SMTP",
                   return_value=MagicMock()):
            imap_client.send_email(
                to=["a@x.com"], subject="s", body="b",
                attachments=[str(good)],
            )

    def test_allowlist_resolves_symlinks(self, imap_client, tmp_path):
        allowed = tmp_path / "allowed"
        outside = tmp_path / "outside"
        allowed.mkdir()
        outside.mkdir()
        target = outside / "secret.txt"
        target.write_text("nope")
        link = allowed / "looks-fine.txt"
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks not supported on this filesystem")
        imap_client.config["security"] = {
            "attachments_allowed_dirs": [str(allowed)],
        }
        with patch("imap_mcp.imap_client.smtplib.SMTP",
                   return_value=MagicMock()):
            with pytest.raises(PermissionError):
                imap_client.send_email(
                    to=["a@x.com"], subject="s", body="b",
                    attachments=[str(link)],
                )

    def test_no_allowlist_means_anything_allowed(self, imap_client, tmp_path):
        any_path = tmp_path / "any.txt"
        any_path.write_text("hi")
        imap_client.config["security"] = {}  # no allowlist
        with patch("imap_mcp.imap_client.smtplib.SMTP",
                   return_value=MagicMock()):
            imap_client.send_email(
                to=["a@x.com"], subject="s", body="b",
                attachments=[str(any_path)],
            )
