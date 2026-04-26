"""Tests for action functions (all mocked)."""

import pytest
from imap_mcp.models import EmailHeader
from tests.conftest import make_fetch_response


class TestActions:
    """Test action functions: mark_read, mark_unread, flag_email, unflag_email, move_email, copy_email, archive_email, save_draft."""

    def test_mark_read(self, imap_client, mock_imap_client):
        """Test marking emails as read."""
        result = imap_client.mark_read(uids=[101], mailbox="INBOX")
        assert result is True
        mock_imap_client.add_flags.assert_called_once_with([101], [b"\\Seen"])

    def test_mark_read_multiple(self, imap_client, mock_imap_client):
        """Test marking multiple emails as read."""
        result = imap_client.mark_read(uids=[101, 102, 103], mailbox="INBOX")
        assert result is True
        mock_imap_client.add_flags.assert_called_once_with([101, 102, 103], [b"\\Seen"])

    def test_mark_unread(self, imap_client, mock_imap_client):
        """Test marking emails as unread."""
        result = imap_client.mark_unread(uids=[101], mailbox="INBOX")
        assert result is True
        mock_imap_client.remove_flags.assert_called_once_with([101], [b"\\Seen"])

    def test_flag_email(self, imap_client, mock_imap_client):
        """Test adding flag to emails."""
        result = imap_client.flag_email(uids=[101], flag="\\Flagged", mailbox="INBOX")
        assert result is True
        mock_imap_client.add_flags.assert_called_once_with([101], [b"\\Flagged"])

    def test_unflag_email(self, imap_client, mock_imap_client):
        """Test removing flag from emails."""
        result = imap_client.unflag_email(uids=[101], flag="\\Flagged", mailbox="INBOX")
        assert result is True
        mock_imap_client.remove_flags.assert_called_once_with([101], [b"\\Flagged"])

    def test_move_email(self, imap_client, mock_imap_client):
        """Test moving emails to another mailbox."""
        result = imap_client.move_email(uids=[101], destination="Archive", mailbox="INBOX")
        assert result is True
        mock_imap_client.move.assert_called_once_with([101], "Archive")

    def test_move_email_namespace_retry(self, imap_client, mock_imap_client):
        """Test moving emails with namespace retry."""
        mock_imap_client.move.side_effect = [
            Exception("namespace error"),
            True,
        ]
        result = imap_client.move_email(uids=[101], destination="Archive", mailbox="INBOX")
        assert result is True
        assert mock_imap_client.move.call_count == 2
        mock_imap_client.move.assert_called_with([101], "INBOX.Archive")

    def test_move_email_unrelated_error_raises(self, imap_client, mock_imap_client):
        """Test that unrelated errors in move are re-raised."""
        mock_imap_client.move.side_effect = Exception("connection lost")
        with pytest.raises(Exception, match="connection lost"):
            imap_client.move_email(uids=[101], destination="Archive")

    def test_copy_email(self, imap_client, mock_imap_client):
        """Test copying email to another folder."""
        result = imap_client.copy_email(uids=[101], destination="Archive", mailbox="INBOX")
        assert result is True
        mock_imap_client.copy.assert_called_once_with([101], "Archive")

    def test_copy_email_namespace_retry(self, imap_client, mock_imap_client):
        """Test copying email with namespace retry."""
        mock_imap_client.copy.side_effect = [
            Exception("namespace error"),
            True,
        ]
        result = imap_client.copy_email(uids=[101], destination="Sent")
        assert result is True
        mock_imap_client.copy.assert_called_with([101], "INBOX.Sent")

    def test_archive_email(self, imap_client, mock_imap_client):
        """Test archiving email (move to Archive folder)."""
        result = imap_client.archive_email(uids=[101], mailbox="INBOX", archive_folder="Archive")
        assert result is True
        mock_imap_client.move.assert_called_once_with([101], "Archive")

    def test_save_draft(self, imap_client, mock_imap_client):
        """Test saving email as draft."""
        result = imap_client.save_draft(
            to=["test@example.com"],
            subject="Test Draft",
            body="This is a test draft.",
            drafts_folder="Drafts",
            include_signature=True,
        )
        assert result["saved"] is True
        assert result["idempotent_replay"] is False
        mock_imap_client.append.assert_called_once()
        call_args = mock_imap_client.append.call_args
        assert call_args[0][0] == "Drafts"
        assert call_args[1]["flags"] == [b"\\Draft"]

    def test_save_draft_without_signature(self, imap_client, mock_imap_client):
        """Test saving draft without signature."""
        result = imap_client.save_draft(
            to=["test@example.com"],
            subject="No Sig Draft",
            body="Plain body without signature.",
            drafts_folder="Drafts",
            include_signature=False,
        )
        assert result["saved"] is True
        # Check the message bytes don't include signature
        call_args = mock_imap_client.append.call_args
        msg_bytes = call_args[0][1]
        assert b"--\nTest User" not in msg_bytes

    def test_save_draft_with_html(self, imap_client, mock_imap_client):
        """Test saving draft with HTML body."""
        result = imap_client.save_draft(
            to=["test@example.com"],
            subject="HTML Draft",
            body="Plain text version",
            html_body="<html><body><h1>HTML Version</h1></body></html>",
            drafts_folder="Drafts",
        )
        assert result["saved"] is True
        call_args = mock_imap_client.append.call_args
        msg_bytes = call_args[0][1]
        assert b"multipart/alternative" in msg_bytes

    def test_save_draft_with_cc_bcc(self, imap_client, mock_imap_client):
        """Test saving draft with CC and BCC."""
        result = imap_client.save_draft(
            to=["test@example.com"],
            subject="CC/BCC Draft",
            body="Test with CC and BCC",
            cc=["cc@example.com"],
            bcc=["bcc@example.com"],
            drafts_folder="Drafts",
        )
        assert result["saved"] is True
        call_args = mock_imap_client.append.call_args
        msg_bytes = call_args[0][1]
        assert b"cc@example.com" in msg_bytes
        assert b"bcc@example.com" in msg_bytes

    def test_save_draft_namespace_retry(self, imap_client, mock_imap_client):
        """Test saving draft with namespace retry."""
        mock_imap_client.append.side_effect = [
            Exception("no such mailbox"),
            True,
        ]
        result = imap_client.save_draft(
            to=["test@example.com"],
            subject="Draft",
            body="body",
            drafts_folder="Drafts",
        )
        assert result["saved"] is True
        assert mock_imap_client.append.call_count == 2
        second_call = mock_imap_client.append.call_args_list[1]
        assert second_call[0][0] == "INBOX.Drafts"

    def test_save_draft_from_header(self, imap_client, mock_imap_client):
        """Test that save_draft sets the From header from config."""
        imap_client.save_draft(
            to=["test@example.com"],
            subject="From test",
            body="body",
        )
        call_args = mock_imap_client.append.call_args
        msg_bytes = call_args[0][1]
        assert b"Test User" in msg_bytes
        assert b"user@example.com" in msg_bytes
