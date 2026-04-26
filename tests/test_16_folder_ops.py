"""Tests for folder management: rename, delete, empty, subscribe, unsubscribe."""

import pytest

from imap_mcp.models import MailboxInfo


class TestRenameMailbox:
    def test_rename_basic(self, imap_client, mock_imap_client):
        result = imap_client.rename_mailbox("Old", "New")
        assert result == {"renamed": True, "from": "Old", "to": "New"}
        mock_imap_client.rename_folder.assert_called_once_with("Old", "New")

    def test_rename_namespace_retry(self, imap_client, mock_imap_client):
        mock_imap_client.rename_folder.side_effect = [
            Exception("namespace"), True,
        ]
        result = imap_client.rename_mailbox("Old", "New")
        assert result["from"] == "INBOX.Old"
        assert result["to"] == "INBOX.New"
        assert mock_imap_client.rename_folder.call_count == 2


class TestDeleteMailbox:
    def test_delete_basic(self, imap_client, mock_imap_client):
        result = imap_client.delete_mailbox("Junk")
        assert result == {"deleted": True, "mailbox": "Junk"}
        mock_imap_client.delete_folder.assert_called_once_with("Junk")

    def test_delete_namespace_retry(self, imap_client, mock_imap_client):
        mock_imap_client.delete_folder.side_effect = [
            Exception("no such mailbox"), True,
        ]
        result = imap_client.delete_mailbox("Junk")
        assert result["mailbox"] == "INBOX.Junk"


class TestEmptyMailbox:
    def test_empty_mailbox_with_messages(self, imap_client, mock_imap_client):
        mock_imap_client.search.return_value = [10, 11, 12]
        result = imap_client.empty_mailbox("Trash")
        assert result["deleted_count"] == 3
        mock_imap_client.add_flags.assert_called_once_with([10, 11, 12], [b"\\Deleted"])
        mock_imap_client.expunge.assert_called_once()

    def test_empty_mailbox_already_empty(self, imap_client, mock_imap_client):
        mock_imap_client.search.return_value = []
        result = imap_client.empty_mailbox("Trash")
        assert result["deleted_count"] == 0
        mock_imap_client.add_flags.assert_not_called()
        mock_imap_client.expunge.assert_not_called()


class TestSubscribeMailbox:
    def test_subscribe_unsubscribe(self, imap_client, mock_imap_client):
        result = imap_client.subscribe_mailbox("Newsletters")
        assert result == {"subscribed": True, "mailbox": "Newsletters"}
        mock_imap_client.subscribe_folder.assert_called_once_with("Newsletters")

        result = imap_client.unsubscribe_mailbox("Newsletters")
        assert result == {"unsubscribed": True, "mailbox": "Newsletters"}
        mock_imap_client.unsubscribe_folder.assert_called_once_with("Newsletters")

    def test_subscribe_namespace_retry(self, imap_client, mock_imap_client):
        mock_imap_client.subscribe_folder.side_effect = [Exception("no such"), True]
        result = imap_client.subscribe_mailbox("Newsletters")
        assert result["mailbox"] == "INBOX.Newsletters"


class TestListSubscribedMailboxes:
    def test_list_subscribed(self, imap_client, mock_imap_client):
        mock_imap_client.list_sub_folders.return_value = [
            ((b"\\HasNoChildren",), b"/", "INBOX"),
            ((b"\\HasNoChildren",), b"/", "Newsletters"),
        ]
        result = imap_client.list_subscribed_mailboxes(pattern="*")
        assert len(result) == 2
        assert all(isinstance(m, MailboxInfo) for m in result)
        names = {m.name for m in result}
        assert names == {"INBOX", "Newsletters"}
        mock_imap_client.list_sub_folders.assert_called_once_with(pattern="*")
