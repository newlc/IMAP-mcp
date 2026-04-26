"""Tests for INBOX. namespace prefix retry logic.

Several methods retry operations with an 'INBOX.' prefix when the server
requires namespaced folder names.  This file consolidates those tests.
"""

import pytest
from tests.conftest import SELECT_FOLDER_RESPONSE


class TestNamespaceRetry:
    """Test namespace retry in create_mailbox, select_mailbox, move_email, copy_email, save_draft."""

    # --- select_mailbox ---

    def test_select_mailbox_retries_with_prefix(self, imap_client, mock_imap_client):
        """select_mailbox retries with INBOX. prefix on failure."""
        mock_imap_client.select_folder.side_effect = [
            Exception("Mailbox not found"),
            dict(SELECT_FOLDER_RESPONSE),
        ]
        result = imap_client.select_mailbox("Drafts")
        assert result.name == "INBOX.Drafts"
        assert imap_client.current_mailbox == "INBOX.Drafts"

    def test_select_mailbox_no_retry_for_inbox(self, imap_client, mock_imap_client):
        """select_mailbox does NOT retry with prefix when folder is INBOX."""
        mock_imap_client.select_folder.side_effect = Exception("Error")
        with pytest.raises(Exception):
            imap_client.select_mailbox("INBOX")

    def test_select_mailbox_no_retry_for_prefixed(self, imap_client, mock_imap_client):
        """select_mailbox does NOT retry when folder already starts with INBOX."""
        mock_imap_client.select_folder.side_effect = Exception("Error")
        with pytest.raises(Exception):
            imap_client.select_mailbox("INBOX.Drafts")

    # --- create_mailbox ---

    def test_create_mailbox_retries_on_namespace_error(self, imap_client, mock_imap_client):
        """create_mailbox retries with INBOX. prefix on namespace error."""
        mock_imap_client.create_folder.side_effect = [
            Exception("namespace error"),
            True,
        ]
        result = imap_client.create_mailbox("Projects")
        assert result is True
        mock_imap_client.create_folder.assert_called_with("INBOX.Projects")

    def test_create_mailbox_retries_on_no_such_error(self, imap_client, mock_imap_client):
        """create_mailbox retries on 'no such' error."""
        mock_imap_client.create_folder.side_effect = [
            Exception("no such mailbox"),
            True,
        ]
        result = imap_client.create_mailbox("Archive")
        assert result is True
        mock_imap_client.create_folder.assert_called_with("INBOX.Archive")

    def test_create_mailbox_retries_on_mailbox_error(self, imap_client, mock_imap_client):
        """create_mailbox retries on 'mailbox' error."""
        mock_imap_client.create_folder.side_effect = [
            Exception("invalid mailbox name"),
            True,
        ]
        result = imap_client.create_mailbox("test")
        assert result is True

    def test_create_mailbox_does_not_retry_unrelated(self, imap_client, mock_imap_client):
        """create_mailbox does NOT retry for unrelated errors (e.g. connection)."""
        mock_imap_client.create_folder.side_effect = Exception("connection lost")
        with pytest.raises(Exception, match="connection lost"):
            imap_client.create_mailbox("Folder")

    # --- move_email ---

    def test_move_email_retries_on_namespace_error(self, imap_client, mock_imap_client):
        """move_email retries with INBOX. prefix on namespace error."""
        mock_imap_client.move.side_effect = [
            Exception("namespace error"),
            True,
        ]
        result = imap_client.move_email(uids=[1], destination="Archive")
        assert result is True
        mock_imap_client.move.assert_called_with([1], "INBOX.Archive")

    def test_move_email_no_retry_for_prefixed(self, imap_client, mock_imap_client):
        """move_email does NOT retry when destination already starts with INBOX."""
        mock_imap_client.move.side_effect = Exception("namespace error")
        with pytest.raises(Exception):
            imap_client.move_email(uids=[1], destination="INBOX.Archive")

    def test_move_email_does_not_retry_unrelated(self, imap_client, mock_imap_client):
        """move_email does NOT retry for unrelated errors."""
        mock_imap_client.move.side_effect = Exception("timeout")
        with pytest.raises(Exception, match="timeout"):
            imap_client.move_email(uids=[1], destination="Archive")

    # --- copy_email ---

    def test_copy_email_retries_on_namespace_error(self, imap_client, mock_imap_client):
        """copy_email retries with INBOX. prefix on namespace error."""
        mock_imap_client.copy.side_effect = [
            Exception("namespace error"),
            True,
        ]
        result = imap_client.copy_email(uids=[1], destination="Sent")
        assert result is True
        mock_imap_client.copy.assert_called_with([1], "INBOX.Sent")

    def test_copy_email_no_retry_for_prefixed(self, imap_client, mock_imap_client):
        """copy_email does NOT retry when destination already starts with INBOX."""
        mock_imap_client.copy.side_effect = Exception("namespace error")
        with pytest.raises(Exception):
            imap_client.copy_email(uids=[1], destination="INBOX.Sent")

    # --- save_draft ---

    def test_save_draft_retries_on_no_such_mailbox(self, imap_client, mock_imap_client):
        """save_draft retries with INBOX.Drafts on 'no such' error."""
        mock_imap_client.append.side_effect = [
            Exception("no such mailbox"),
            True,
        ]
        result = imap_client.save_draft(
            to=["x@y.com"], subject="Test", body="body", drafts_folder="Drafts"
        )
        assert result["saved"] is True
        second_call = mock_imap_client.append.call_args_list[1]
        assert second_call[0][0] == "INBOX.Drafts"

    def test_save_draft_retries_on_namespace_error(self, imap_client, mock_imap_client):
        """save_draft retries with INBOX. prefix on namespace error."""
        mock_imap_client.append.side_effect = [
            Exception("namespace error"),
            True,
        ]
        result = imap_client.save_draft(
            to=["x@y.com"], subject="Test", body="body", drafts_folder="Drafts"
        )
        assert result["saved"] is True

    def test_save_draft_does_not_retry_unrelated(self, imap_client, mock_imap_client):
        """save_draft does NOT retry for unrelated errors."""
        mock_imap_client.append.side_effect = Exception("connection lost")
        with pytest.raises(Exception, match="connection lost"):
            imap_client.save_draft(
                to=["x@y.com"], subject="Test", body="body", drafts_folder="Drafts"
            )
