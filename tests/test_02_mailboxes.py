"""Tests for mailbox functions (all mocked)."""

import pytest
from imap_mcp.models import MailboxInfo, MailboxStatus


class TestMailboxes:
    """Test mailbox functions: list_mailboxes, select_mailbox, create_mailbox, get_mailbox_status."""

    def test_list_mailboxes(self, imap_client, mock_imap_client):
        """Test listing all mailboxes."""
        result = imap_client.list_mailboxes()
        assert isinstance(result, dict)
        assert result["total"] == 8
        assert result["next_cursor"] is None
        mboxes = result["mailboxes"]
        assert len(mboxes) == 8
        assert all(isinstance(m, MailboxInfo) for m in mboxes)
        assert any(m.name == "INBOX" for m in mboxes)

    def test_list_mailboxes_with_pattern(self, imap_client, mock_imap_client):
        """Test listing mailboxes with pattern."""
        mock_imap_client.list_folders.return_value = [
            ((b"\\HasNoChildren",), "/", "INBOX"),
        ]
        result = imap_client.list_mailboxes(pattern="INBOX*")
        assert isinstance(result, dict)
        assert result["total"] == 1
        assert len(result["mailboxes"]) == 1
        mock_imap_client.list_folders.assert_called_with(pattern="INBOX*")

    def test_select_mailbox(self, imap_client, mock_imap_client):
        """Test selecting a mailbox."""
        result = imap_client.select_mailbox("INBOX")
        assert isinstance(result, MailboxStatus)
        assert result.name == "INBOX"
        assert result.exists == 150
        assert result.recent == 3
        assert result.unseen == 10
        assert result.uidnext == 44832
        assert result.uidvalidity == 1
        assert imap_client.current_mailbox == "INBOX"

    def test_select_nonexistent_mailbox_raises(self, imap_client, mock_imap_client):
        """Test that selecting non-existent mailbox raises when both attempts fail."""
        mock_imap_client.select_folder.side_effect = Exception("Mailbox not found")
        with pytest.raises(Exception):
            imap_client.select_mailbox("NONEXISTENT_FOLDER_12345")

    def test_select_mailbox_namespace_retry(self, imap_client, mock_imap_client):
        """Test that selecting a mailbox retries with INBOX. prefix."""
        from tests.conftest import SELECT_FOLDER_RESPONSE

        # First call fails, second (with INBOX. prefix) succeeds
        mock_imap_client.select_folder.side_effect = [
            Exception("Mailbox not found"),
            dict(SELECT_FOLDER_RESPONSE),
        ]
        result = imap_client.select_mailbox("Drafts")
        assert isinstance(result, MailboxStatus)
        assert result.name == "INBOX.Drafts"
        assert imap_client.current_mailbox == "INBOX.Drafts"

    def test_select_mailbox_inbox_no_retry(self, imap_client, mock_imap_client):
        """Test that INBOX itself does not get INBOX. prefix retry."""
        mock_imap_client.select_folder.side_effect = Exception("INBOX error")
        with pytest.raises(Exception):
            imap_client.select_mailbox("INBOX")

    def test_get_mailbox_status(self, imap_client, mock_imap_client):
        """Test getting mailbox status."""
        result = imap_client.get_mailbox_status("INBOX")
        assert isinstance(result, MailboxStatus)
        assert result.name == "INBOX"
        assert result.exists == 150
        assert result.unseen == 10
        assert result.uidnext == 44832
        assert result.uidvalidity == 1
        mock_imap_client.folder_status.assert_called_with(
            "INBOX", ["MESSAGES", "RECENT", "UNSEEN", "UIDNEXT", "UIDVALIDITY"]
        )

    def test_create_mailbox(self, imap_client, mock_imap_client):
        """Test creating a new mailbox."""
        result = imap_client.create_mailbox("TestFolder")
        assert result is True
        mock_imap_client.create_folder.assert_called_once_with("TestFolder")

    def test_create_mailbox_namespace_retry(self, imap_client, mock_imap_client):
        """Test creating mailbox with namespace retry."""
        mock_imap_client.create_folder.side_effect = [
            Exception("namespace error"),
            True,
        ]
        result = imap_client.create_mailbox("TestFolder")
        assert result is True
        assert mock_imap_client.create_folder.call_count == 2
        mock_imap_client.create_folder.assert_called_with("INBOX.TestFolder")

    def test_create_mailbox_unrelated_error_raises(self, imap_client, mock_imap_client):
        """Test that unrelated errors during create_mailbox are re-raised."""
        mock_imap_client.create_folder.side_effect = Exception("connection lost")
        with pytest.raises(Exception, match="connection lost"):
            imap_client.create_mailbox("TestFolder")
