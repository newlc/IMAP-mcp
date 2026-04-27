"""Tests for the polish-bundle fixes:

* forward_email cache reuse when body+attachments are in the local cache.
* accounts_health quota threshold (>95% -> ok=false).
* html2text format option (markdown vs plain).
* Gmail label dedup in archive_email.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from imap_mcp.cache import EmailCache
from imap_mcp.mail_utils import html_to_plain, strip_markdown


# ===========================================================================
# forward_email cache reuse
# ===========================================================================


class TestForwardCacheReuse:
    @pytest.fixture
    def with_cached_email(self, imap_client, tmp_cache_db):
        cache = EmailCache(tmp_cache_db, encrypted=False, keyring_username="t-fwd")
        imap_client.email_cache = cache
        cache.store_email(
            "INBOX", 7,
            {
                "message_id": "<orig@x>", "subject": "Cached original",
                "from_address": {"email": "alice@x.com", "name": "Alice"},
                "to_addresses": [{"email": "user@example.com", "name": None}],
                "cc_addresses": [],
                "date": datetime(2026, 4, 1).isoformat(),
                "flags": [], "size": 100,
            },
            {"text": "Body of the original mail", "html": None},
        )
        cache.store_attachment(
            "INBOX", 7, 0, "spec.pdf", "application/pdf", 4,
            b"%PDF-fake-bytes",
        )
        return imap_client

    def test_forward_uses_cache_no_imap_fetch(self, with_cached_email, mock_imap_client):
        # If the cache path is taken, client.fetch must not be called.
        smtp_instance = MagicMock()
        with patch("imap_mcp.imap_client.smtplib.SMTP", return_value=smtp_instance):
            result = with_cached_email.forward_email(
                uid=7, to=["fwd@example.com"], mailbox="INBOX",
            )
        assert result["forward_source"] == "cache"
        # No body fetch happened.
        for call in mock_imap_client.fetch.call_args_list:
            fields = call.args[1] if len(call.args) >= 2 else call.kwargs.get("data", [])
            assert b"BODY[]" not in [f.encode() if isinstance(f, str) else f for f in fields]
        # SMTP got called with attachment + cached body. Body is base64 in
        # the wire format; decode the text/plain part to check.
        _, _, raw = smtp_instance.sendmail.call_args[0]
        assert b'filename="spec.pdf"' in raw

        import email as _email
        msg = _email.message_from_bytes(raw)
        text_body = ""
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                text_body = part.get_payload(decode=True).decode("utf-8", errors="replace")
                break
        assert "Body of the original mail" in text_body

    def test_forward_falls_back_to_imap_when_cache_misses_body(
        self, imap_client, mock_imap_client, tmp_cache_db,
    ):
        cache = EmailCache(tmp_cache_db, encrypted=False, keyring_username="t-fwd2")
        imap_client.email_cache = cache
        # Header-only cache row -- no body.
        cache.store_email(
            "INBOX", 8,
            {"message_id": "<x@x>", "subject": "Header-only",
             "from_address": {"email": "a@x"}, "to_addresses": [],
             "cc_addresses": [], "date": None, "flags": [], "size": 0},
        )

        # IMAP fetch returns the real RFC822 body.
        from tests.conftest import MULTIPART_BODY
        mock_imap_client.fetch.side_effect = lambda uids, fields: {
            uids[0]: {b"BODY[]": MULTIPART_BODY}
        }
        smtp_instance = MagicMock()
        with patch("imap_mcp.imap_client.smtplib.SMTP", return_value=smtp_instance):
            result = imap_client.forward_email(
                uid=8, to=["fwd@example.com"], mailbox="INBOX",
            )
        assert result["forward_source"] == "imap"
        mock_imap_client.fetch.assert_called()  # IMAP path engaged

    def test_forward_falls_back_when_attachment_bytes_missing(
        self, imap_client, mock_imap_client, tmp_cache_db,
    ):
        cache = EmailCache(tmp_cache_db, encrypted=False, keyring_username="t-fwd3")
        imap_client.email_cache = cache
        # Email body cached, attachment metadata only -- no bytes. Forwarding
        # without bytes would silently drop the attachment, so we should
        # fall back to IMAP.
        cache.store_email(
            "INBOX", 9,
            {"message_id": "<y@x>", "subject": "Body but no att bytes",
             "from_address": {"email": "a@x"}, "to_addresses": [],
             "cc_addresses": [], "date": None, "flags": [], "size": 0},
            {"text": "body", "html": None},
        )
        # Insert attachment metadata directly without bytes.
        cache.conn.execute(
            "INSERT INTO attachments (mailbox, uid, idx, filename, content_type, size, data) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("INBOX", 9, 0, "thing.bin", "application/octet-stream", 100, None),
        )
        cache.conn.commit()

        from tests.conftest import MULTIPART_BODY
        mock_imap_client.fetch.side_effect = lambda uids, fields: {
            uids[0]: {b"BODY[]": MULTIPART_BODY}
        }
        with patch("imap_mcp.imap_client.smtplib.SMTP", return_value=MagicMock()):
            result = imap_client.forward_email(
                uid=9, to=["fwd@example.com"], mailbox="INBOX",
            )
        assert result["forward_source"] == "imap"

    def test_forward_skip_attachments_uses_cache(self, with_cached_email):
        """include_attachments=False should still hit the cache path -- no
        attachment bytes are needed."""
        smtp_instance = MagicMock()
        with patch("imap_mcp.imap_client.smtplib.SMTP", return_value=smtp_instance):
            result = with_cached_email.forward_email(
                uid=7, to=["fwd@example.com"], mailbox="INBOX",
                include_attachments=False,
            )
        assert result["forward_source"] == "cache"


# ===========================================================================
# accounts_health quota threshold
# ===========================================================================


class TestQuotaThreshold:
    def test_health_check_quota_below_threshold(self, imap_client, mock_imap_client):
        mock_imap_client.noop.return_value = b"OK"
        from types import SimpleNamespace
        mock_imap_client.get_quota_root.return_value = (
            [SimpleNamespace(mailbox=b"INBOX", quota_root=b"")],
            [SimpleNamespace(quota_root=b"", resource=b"STORAGE",
                             usage=500_000, limit=1_000_000)],
        )
        result = imap_client.health_check()
        assert result["ok"] is True
        assert result["quota"]["usage_pct"] == 50.0

    def test_health_check_quota_above_threshold(self, imap_client, mock_imap_client):
        mock_imap_client.noop.return_value = b"OK"
        from types import SimpleNamespace
        mock_imap_client.get_quota_root.return_value = (
            [SimpleNamespace(mailbox=b"INBOX", quota_root=b"")],
            [SimpleNamespace(quota_root=b"", resource=b"STORAGE",
                             usage=970_000, limit=1_000_000)],
        )
        result = imap_client.health_check()
        assert result["ok"] is False
        assert "quota" in result["reason"].lower()
        assert result["quota"]["usage_pct"] == 97.0

    def test_health_check_quota_exactly_at_threshold(self, imap_client, mock_imap_client):
        mock_imap_client.noop.return_value = b"OK"
        from types import SimpleNamespace
        # Exactly 95% -> still triggers the warning (>= threshold).
        mock_imap_client.get_quota_root.return_value = (
            [SimpleNamespace(mailbox=b"INBOX", quota_root=b"")],
            [SimpleNamespace(quota_root=b"", resource=b"STORAGE",
                             usage=950_000, limit=1_000_000)],
        )
        result = imap_client.health_check()
        assert result["ok"] is False

    def test_health_check_quota_disabled(self, imap_client, mock_imap_client):
        mock_imap_client.noop.return_value = b"OK"
        result = imap_client.health_check(check_quota=False)
        # quota field absent or None
        assert result.get("quota") is None
        assert result["ok"] is True
        # No quota lookup happened.
        mock_imap_client.get_quota_root.assert_not_called()

    def test_health_check_quota_unsupported_server(self, imap_client, mock_imap_client):
        mock_imap_client.noop.return_value = b"OK"
        mock_imap_client.get_quota_root.side_effect = Exception("not supported")
        result = imap_client.health_check()
        # Quota lookup failed -> we don't penalize the account.
        assert result["ok"] is True
        assert result["quota"] is None


# ===========================================================================
# html2text format option
# ===========================================================================


class TestHtmlFormat:
    def test_markdown_keeps_links(self):
        out = html_to_plain('<a href="https://x.com">click here</a>', format="markdown")
        assert "click here" in out
        assert "x.com" in out  # link target preserved

    def test_plain_strips_links(self):
        out = html_to_plain('<a href="https://x.com">click here</a>', format="plain")
        assert "click here" in out
        assert "x.com" not in out  # URL gone

    def test_markdown_keeps_emphasis(self):
        out = html_to_plain("<p>This is <strong>important</strong></p>", format="markdown")
        assert "important" in out
        # html2text writes **bold** for <strong>
        assert "**" in out

    def test_plain_strips_emphasis(self):
        out = html_to_plain("<p>This is <strong>important</strong></p>", format="plain")
        assert "important" in out
        assert "**" not in out

    def test_invalid_format_rejected(self):
        with pytest.raises(ValueError):
            html_to_plain("<p>x</p>", format="bogus")

    def test_strip_markdown_helper(self):
        text = "Check **this link**: [click here](https://x.com) and __that__."
        out = strip_markdown(text)
        assert "**" not in out
        assert "__" not in out
        assert "https://x.com" not in out
        assert "click here" in out
        assert "this link" in out
        assert "that" in out

    def test_strip_markdown_handles_headings_and_code(self):
        text = "# Title\n## Subtitle\nUse `the_var` carefully."
        out = strip_markdown(text)
        assert not out.startswith("#")
        assert "Title" in out
        assert "the_var" in out
        assert "`" not in out


class TestSummaryFormat:
    def test_summary_markdown_keeps_links_in_snippet(
        self, imap_client, mock_imap_client, tmp_cache_db,
    ):
        cache = EmailCache(tmp_cache_db, encrypted=False, keyring_username="t-fmt")
        imap_client.email_cache = cache
        cache.store_email(
            "INBOX", 1,
            {"message_id": "<a@x>", "subject": "x",
             "from_address": {"email": "a@x"}, "to_addresses": [],
             "cc_addresses": [], "date": None, "flags": [], "size": 0},
            {"text": "See [docs](https://example.com/docs) please.", "html": None},
        )
        result = imap_client.get_email_summary(uids=[1], mailbox="INBOX",
                                               format="markdown")
        assert "[docs]" in result[0]["snippet"]

    def test_summary_plain_strips_markdown(
        self, imap_client, mock_imap_client, tmp_cache_db,
    ):
        cache = EmailCache(tmp_cache_db, encrypted=False, keyring_username="t-fmt2")
        imap_client.email_cache = cache
        cache.store_email(
            "INBOX", 1,
            {"message_id": "<a@x>", "subject": "x",
             "from_address": {"email": "a@x"}, "to_addresses": [],
             "cc_addresses": [], "date": None, "flags": [], "size": 0},
            {"text": "See [docs](https://example.com/docs) please.", "html": None},
        )
        result = imap_client.get_email_summary(uids=[1], mailbox="INBOX",
                                               format="plain")
        snippet = result[0]["snippet"]
        assert "[docs]" not in snippet
        assert "https://" not in snippet
        assert "docs" in snippet

    def test_summary_invalid_format_rejected(self, imap_client):
        with pytest.raises(ValueError):
            imap_client.get_email_summary(uids=[1], format="lol")


# ===========================================================================
# Gmail label dedup in archive_email
# ===========================================================================


class TestGmailLabelDedup:
    def test_archive_on_gmail_strips_inbox_label(self, imap_client, mock_imap_client):
        mock_imap_client.capabilities.return_value = (b"IMAP4REV1", b"X-GM-EXT-1")
        imap_client.archive_email(uids=[100, 101], mailbox="INBOX")
        # Gmail-specific path: STORE -X-GM-LABELS \Inbox via remove_gmail_labels.
        mock_imap_client.remove_gmail_labels.assert_called_once_with(
            [100, 101], ["\\Inbox"]
        )
        # Plain MOVE was NOT called.
        mock_imap_client.move.assert_not_called()

    def test_archive_on_non_gmail_uses_move(self, imap_client, mock_imap_client):
        mock_imap_client.capabilities.return_value = (b"IMAP4REV1",)
        imap_client.archive_email(uids=[100], mailbox="INBOX")
        mock_imap_client.move.assert_called_once_with([100], "Archive")
        mock_imap_client.remove_gmail_labels.assert_not_called()

    def test_archive_on_gmail_with_custom_folder_uses_move(
        self, imap_client, mock_imap_client,
    ):
        # When the caller asks for a specific folder (not the bare default),
        # MOVE is the right primitive even on Gmail.
        mock_imap_client.capabilities.return_value = (b"IMAP4REV1", b"X-GM-EXT-1")
        imap_client.archive_email(uids=[1], mailbox="INBOX",
                                  archive_folder="[Gmail]/All Mail")
        mock_imap_client.move.assert_called_once()

    def test_archive_dedup_disabled_via_config(self, imap_client, mock_imap_client):
        mock_imap_client.capabilities.return_value = (b"IMAP4REV1", b"X-GM-EXT-1")
        imap_client.config["imap"]["gmail_label_dedup"] = False
        imap_client.archive_email(uids=[1], mailbox="INBOX")
        # Forced regular MOVE.
        mock_imap_client.move.assert_called_once()
        mock_imap_client.remove_gmail_labels.assert_not_called()

    def test_is_gmail_cached(self, imap_client, mock_imap_client):
        mock_imap_client.capabilities.return_value = (b"IMAP4REV1", b"X-GM-EXT-1")
        assert imap_client._is_gmail() is True
        assert imap_client._is_gmail() is True  # cached
        assert mock_imap_client.capabilities.call_count == 1


# ===========================================================================
# Sanity check that update_draft/delete_draft still work after move
# ===========================================================================


class TestDraftMoveSmoke:
    def test_update_draft_after_move(self, imap_client, mock_imap_client):
        mock_imap_client.append.return_value = b"OK [APPENDUID 1 99] (Success)"
        result = imap_client.update_draft(
            uid=42, to=["a@x"], subject="s", body="b",
        )
        assert result["updated"] is True
        assert result["new_uid"] == 99

    def test_delete_draft_after_move(self, imap_client, mock_imap_client):
        result = imap_client.delete_draft(uid=42)
        assert result["deleted"] is True
        mock_imap_client.add_flags.assert_called_with([42], [b"\\Deleted"])
        mock_imap_client.expunge.assert_called_once()
