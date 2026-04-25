"""Tests for search functions (all mocked)."""

import pytest
from imap_mcp.models import EmailHeader
from tests.conftest import make_fetch_response, make_envelope


class TestSearch:
    """Test search functions: search_emails, search_by_sender, search_by_subject, search_by_date, search_unread, search_flagged."""

    def test_search_emails_text(self, imap_client, mock_imap_client):
        """Test searching emails by text."""
        mock_imap_client.search.return_value = [101, 102]
        result = imap_client.search_emails(query="meeting", mailbox="INBOX", limit=10)
        assert isinstance(result, list)
        assert len(result) == 2
        assert all(isinstance(e, EmailHeader) for e in result)
        mock_imap_client.search.assert_called_with(["TEXT", "meeting"])

    def test_search_emails_imap_syntax(self, imap_client, mock_imap_client):
        """Test searching with IMAP SEARCH syntax (FROM keyword)."""
        mock_imap_client.search.return_value = [101]
        result = imap_client.search_emails(query="FROM alice@example.com", mailbox="INBOX", limit=5)
        assert isinstance(result, list)

    def test_search_emails_all(self, imap_client, mock_imap_client):
        """Test searching with ALL keyword."""
        mock_imap_client.search.return_value = [101, 102, 103]
        result = imap_client.search_emails(query="ALL", mailbox="INBOX", limit=5)
        assert isinstance(result, list)

    def test_search_emails_empty_result(self, imap_client, mock_imap_client):
        """Test searching with no results."""
        mock_imap_client.search.return_value = []
        result = imap_client.search_emails(query="nonexistent", mailbox="INBOX")
        assert result == []

    def test_search_by_sender(self, imap_client, mock_imap_client):
        """Test searching by sender address."""
        mock_imap_client.search.return_value = [101, 102]
        result = imap_client.search_by_sender(sender="alice@example.com", mailbox="INBOX", limit=10)
        assert isinstance(result, list)
        assert len(result) == 2
        mock_imap_client.search.assert_called_with(["FROM", "alice@example.com"])

    def test_search_by_subject(self, imap_client, mock_imap_client):
        """Test searching by subject."""
        mock_imap_client.search.return_value = [103]
        result = imap_client.search_by_subject(subject="meeting", mailbox="INBOX", limit=10)
        assert isinstance(result, list)
        assert len(result) == 1
        mock_imap_client.search.assert_called_with(["SUBJECT", "meeting"])

    def test_search_by_date_since(self, imap_client, mock_imap_client):
        """Test searching by date (since)."""
        mock_imap_client.search.return_value = [101, 102]
        result = imap_client.search_by_date(since="2024-01-01", mailbox="INBOX", limit=10)
        assert isinstance(result, list)
        assert all(isinstance(e, EmailHeader) for e in result)
        call_args = mock_imap_client.search.call_args[0][0]
        assert "SINCE" in call_args
        assert "01-Jan-2024" in call_args

    def test_search_by_date_before(self, imap_client, mock_imap_client):
        """Test searching by date (before)."""
        mock_imap_client.search.return_value = [101]
        result = imap_client.search_by_date(before="2026-12-31", mailbox="INBOX", limit=10)
        assert isinstance(result, list)
        call_args = mock_imap_client.search.call_args[0][0]
        assert "BEFORE" in call_args

    def test_search_by_date_range(self, imap_client, mock_imap_client):
        """Test searching by date range."""
        mock_imap_client.search.return_value = [101, 102]
        result = imap_client.search_by_date(
            since="2024-01-01", before="2026-12-31", mailbox="INBOX", limit=10
        )
        assert isinstance(result, list)
        call_args = mock_imap_client.search.call_args[0][0]
        assert "SINCE" in call_args
        assert "BEFORE" in call_args

    def test_search_by_date_no_criteria(self, imap_client, mock_imap_client):
        """Test searching by date with no since/before uses ALL."""
        mock_imap_client.search.return_value = [101]
        result = imap_client.search_by_date(mailbox="INBOX", limit=10)
        mock_imap_client.search.assert_called_with(["ALL"])

    def test_search_unread(self, imap_client, mock_imap_client):
        """Test searching unread emails."""
        mock_imap_client.search.return_value = [104, 105]
        mock_imap_client.fetch.side_effect = lambda uids, fields: make_fetch_response(
            uids, flags=()  # No \\Seen flag = unread
        )
        result = imap_client.search_unread(mailbox="INBOX", limit=10)
        assert isinstance(result, list)
        assert len(result) == 2
        assert all(isinstance(e, EmailHeader) for e in result)
        for email_hdr in result:
            assert "\\Seen" not in email_hdr.flags
        mock_imap_client.search.assert_called_with(["UNSEEN"])

    def test_search_flagged(self, imap_client, mock_imap_client):
        """Test searching flagged emails."""
        mock_imap_client.search.return_value = [101]
        mock_imap_client.fetch.side_effect = lambda uids, fields: make_fetch_response(
            uids, flags=(b"\\Seen", b"\\Flagged")
        )
        result = imap_client.search_flagged(mailbox="INBOX", limit=10)
        assert isinstance(result, list)
        assert len(result) == 1
        for email_hdr in result:
            assert "\\Flagged" in email_hdr.flags
        mock_imap_client.search.assert_called_with(["FLAGGED"])

    def test_search_with_limit(self, imap_client, mock_imap_client):
        """Test that search respects limit."""
        mock_imap_client.search.return_value = list(range(1, 100))
        result = imap_client.search_emails(query="ALL", mailbox="INBOX", limit=3)
        assert len(result) <= 3

    def test_search_selects_default_mailbox(self, imap_client, mock_imap_client):
        """Test that search selects INBOX when no mailbox specified and none selected."""
        imap_client.current_mailbox = None
        mock_imap_client.search.return_value = [101]
        imap_client.search_emails(query="test")
        mock_imap_client.select_folder.assert_called()

    def test_search_emails_fallback_on_error(self, imap_client, mock_imap_client):
        """Test that search falls back to TEXT search on IMAP error."""
        call_count = [0]
        def search_side_effect(criteria, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise Exception("IMAP error")
            return [101]
        mock_imap_client.search.side_effect = search_side_effect
        result = imap_client.search_emails(query="FROM bob", mailbox="INBOX")
        assert isinstance(result, list)
