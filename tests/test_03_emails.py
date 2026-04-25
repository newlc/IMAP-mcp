"""Tests for email reading functions (all mocked)."""

import pytest
from imap_mcp.models import EmailHeader, Email, EmailBody, Attachment
from tests.conftest import make_fetch_response, make_envelope, MULTIPART_BODY


class TestEmails:
    """Test email functions: fetch_emails, get_email, get_email_headers, get_email_body, get_attachments, get_thread."""

    def test_fetch_emails(self, imap_client, mock_imap_client):
        """Test fetching emails from INBOX."""
        result = imap_client.fetch_emails(mailbox="INBOX", limit=5)
        assert isinstance(result, list)
        assert len(result) == 5
        assert all(isinstance(e, EmailHeader) for e in result)
        for hdr in result:
            assert hdr.uid > 0

    def test_fetch_emails_with_limit(self, imap_client, mock_imap_client):
        """Test fetching emails with limit."""
        mock_imap_client.search.return_value = [101, 102, 103, 104, 105]
        result = imap_client.fetch_emails(mailbox="INBOX", limit=2)
        assert isinstance(result, list)
        assert len(result) <= 2

    def test_fetch_emails_with_offset(self, imap_client, mock_imap_client):
        """Test fetching emails with offset."""
        mock_imap_client.search.return_value = [101, 102, 103, 104, 105]
        result = imap_client.fetch_emails(mailbox="INBOX", limit=2, offset=1)
        assert len(result) <= 2

    def test_fetch_emails_empty_mailbox(self, imap_client, mock_imap_client):
        """Test fetching from empty mailbox returns empty list."""
        mock_imap_client.search.return_value = []
        result = imap_client.fetch_emails(mailbox="INBOX")
        assert result == []

    def test_fetch_emails_with_date_filters(self, imap_client, mock_imap_client):
        """Test fetching with since and before filters."""
        result = imap_client.fetch_emails(
            mailbox="INBOX", since="2024-01-01", before="2026-12-31"
        )
        assert isinstance(result, list)
        # Verify search was called with date criteria
        call_args = mock_imap_client.search.call_args[0][0]
        assert "SINCE" in call_args
        assert "BEFORE" in call_args

    def test_fetch_emails_selects_mailbox(self, imap_client, mock_imap_client):
        """Test that fetch_emails selects the given mailbox."""
        imap_client.current_mailbox = None
        imap_client.fetch_emails(mailbox="Drafts")
        mock_imap_client.select_folder.assert_called()

    def test_get_email(self, imap_client, mock_imap_client):
        """Test getting complete email by UID."""
        mock_imap_client.fetch.side_effect = lambda uids, fields: make_fetch_response(
            uids, include_body=True
        )
        result = imap_client.get_email(uid=101, mailbox="INBOX")
        assert isinstance(result, Email)
        assert result.header.uid == 101
        assert result.body is not None
        assert result.body.text is not None

    def test_get_email_nonexistent_uid(self, imap_client, mock_imap_client):
        """Test getting non-existent email raises ValueError."""
        mock_imap_client.fetch.side_effect = lambda uids, fields: {}
        imap_client.select_mailbox("INBOX")
        with pytest.raises(ValueError, match="not found"):
            imap_client.get_email(uid=999999999)

    def test_get_email_headers(self, imap_client, mock_imap_client):
        """Test getting only email headers."""
        result = imap_client.get_email_headers(uid=101, mailbox="INBOX")
        assert isinstance(result, EmailHeader)
        assert result.uid == 101

    def test_get_email_headers_nonexistent(self, imap_client, mock_imap_client):
        """Test getting headers for non-existent email raises ValueError."""
        mock_imap_client.fetch.side_effect = lambda uids, fields: {}
        with pytest.raises(ValueError, match="not found"):
            imap_client.get_email_headers(uid=999999)

    def test_get_email_body_text(self, imap_client, mock_imap_client):
        """Test getting email body as text."""
        mock_imap_client.fetch.side_effect = lambda uids, fields: {
            uids[0]: {b"BODY[]": b"From: a@b.com\r\nSubject: Hi\r\n\r\nHello world"}
        }
        result = imap_client.get_email_body(uid=101, mailbox="INBOX", format="text")
        assert isinstance(result, str)
        assert "Hello world" in result

    def test_get_email_body_html(self, imap_client, mock_imap_client):
        """Test getting email body as HTML."""
        html_body = (
            b"From: a@b.com\r\nSubject: Hi\r\n"
            b"Content-Type: text/html; charset=utf-8\r\n"
            b"\r\n"
            b"<html><body><h1>Hello</h1></body></html>"
        )
        mock_imap_client.fetch.side_effect = lambda uids, fields: {
            uids[0]: {b"BODY[]": html_body}
        }
        result = imap_client.get_email_body(uid=101, mailbox="INBOX", format="html")
        assert isinstance(result, str)
        assert "<h1>Hello</h1>" in result

    def test_get_email_body_nonexistent(self, imap_client, mock_imap_client):
        """Test getting body of non-existent email raises ValueError."""
        mock_imap_client.fetch.side_effect = lambda uids, fields: {}
        with pytest.raises(ValueError, match="not found"):
            imap_client.get_email_body(uid=999999)

    def test_get_attachments(self, imap_client, mock_imap_client):
        """Test getting attachments list."""
        mock_imap_client.fetch.side_effect = lambda uids, fields: {
            uids[0]: {b"BODY[]": MULTIPART_BODY}
        }
        result = imap_client.get_attachments(uid=101, mailbox="INBOX")
        assert isinstance(result, list)
        assert len(result) == 1
        assert isinstance(result[0], Attachment)
        assert result[0].filename == "report.pdf"
        assert result[0].content_type == "application/pdf"

    def test_get_attachments_no_attachments(self, imap_client, mock_imap_client):
        """Test getting attachments when there are none."""
        mock_imap_client.fetch.side_effect = lambda uids, fields: {
            uids[0]: {
                b"BODY[]": b"From: a@b.com\r\nSubject: Hi\r\n\r\nNo attachments here"
            }
        }
        result = imap_client.get_attachments(uid=101, mailbox="INBOX")
        assert result == []

    def test_get_thread(self, imap_client, mock_imap_client):
        """Test getting email thread."""
        # First call: get_email fetches full email
        # Second call: search by subject
        call_count = [0]
        def fetch_side_effect(uids, fields):
            call_count[0] += 1
            return make_fetch_response(uids, include_body=(call_count[0] == 1))
        mock_imap_client.fetch.side_effect = fetch_side_effect
        mock_imap_client.search.return_value = [101, 102, 103]

        result = imap_client.get_thread(uid=101, mailbox="INBOX")
        assert isinstance(result, list)
        assert len(result) >= 1
        assert all(isinstance(e, EmailHeader) for e in result)

    def test_email_header_parsing(self, imap_client, mock_imap_client):
        """Test that envelope data is correctly parsed into EmailHeader."""
        env = make_envelope(
            subject=b"Important Meeting",
            from_name="Alice",
            from_mailbox="alice",
            from_host="corp.com",
            to_list=None,
            message_id=b"<unique-id-123@corp.com>",
        )
        mock_imap_client.fetch.side_effect = lambda uids, fields: {
            uids[0]: {
                b"ENVELOPE": env,
                b"FLAGS": (b"\\Seen", b"\\Flagged"),
                b"RFC822.SIZE": 8192,
            }
        }
        result = imap_client.get_email_headers(uid=42, mailbox="INBOX")
        assert result.subject == "Important Meeting"
        assert result.from_address.email == "alice@corp.com"
        assert result.from_address.name == "Alice"
        assert result.message_id == "<unique-id-123@corp.com>"
        assert "\\Seen" in result.flags
        assert "\\Flagged" in result.flags
        assert result.size == 8192
