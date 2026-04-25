"""Tests for statistics functions (all mocked)."""

import pytest


class TestStatistics:
    """Test statistics functions: get_unread_count, get_total_count."""

    def test_get_unread_count(self, imap_client, mock_imap_client):
        """Test getting unread email count."""
        mock_imap_client.folder_status.return_value = {b"UNSEEN": 10}
        result = imap_client.get_unread_count(mailbox="INBOX")
        assert isinstance(result, int)
        assert result == 10
        mock_imap_client.folder_status.assert_called_with("INBOX", ["UNSEEN"])

    def test_get_total_count(self, imap_client, mock_imap_client):
        """Test getting total email count."""
        mock_imap_client.folder_status.return_value = {b"MESSAGES": 150}
        result = imap_client.get_total_count(mailbox="INBOX")
        assert isinstance(result, int)
        assert result == 150
        mock_imap_client.folder_status.assert_called_with("INBOX", ["MESSAGES"])

    def test_unread_count_less_or_equal_total(self, imap_client, mock_imap_client):
        """Test that unread count is less than or equal to total count."""
        mock_imap_client.folder_status.side_effect = [
            {b"UNSEEN": 10},
            {b"MESSAGES": 150},
        ]
        unread = imap_client.get_unread_count(mailbox="INBOX")
        total = imap_client.get_total_count(mailbox="INBOX")
        assert unread <= total

    def test_counts_match_status(self, imap_client, mock_imap_client):
        """Test that counts match mailbox status values."""
        from tests.conftest import FOLDER_STATUS_RESPONSE

        mock_imap_client.folder_status.return_value = dict(FOLDER_STATUS_RESPONSE)
        status = imap_client.get_mailbox_status("INBOX")

        mock_imap_client.folder_status.return_value = {b"MESSAGES": 150}
        total = imap_client.get_total_count(mailbox="INBOX")

        mock_imap_client.folder_status.return_value = {b"UNSEEN": 10}
        unread = imap_client.get_unread_count(mailbox="INBOX")

        assert total == status.exists
        assert unread == status.unseen

    def test_get_unread_count_zero(self, imap_client, mock_imap_client):
        """Test getting unread count when it's zero."""
        mock_imap_client.folder_status.return_value = {b"UNSEEN": 0}
        result = imap_client.get_unread_count(mailbox="INBOX")
        assert result == 0

    def test_get_total_count_empty_mailbox(self, imap_client, mock_imap_client):
        """Test getting total count for empty mailbox."""
        mock_imap_client.folder_status.return_value = {b"MESSAGES": 0}
        result = imap_client.get_total_count(mailbox="INBOX")
        assert result == 0
