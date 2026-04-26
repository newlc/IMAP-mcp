"""Tests for draft management, spam tools, and server metadata."""

import pytest
from types import SimpleNamespace
from unittest.mock import MagicMock


# ---------------------------------------------------------------------------
# Drafts
# ---------------------------------------------------------------------------


class TestUpdateDraft:
    def test_update_draft_appends_then_expunges_old(self, imap_client, mock_imap_client):
        mock_imap_client.append.return_value = b"OK [APPENDUID 1 99] (Success)"
        result = imap_client.update_draft(
            uid=42,
            to=["alice@example.com"], subject="Re: Hi", body="Updated text",
        )
        assert result["old_uid"] == 42
        assert result["new_uid"] == 99
        # APPEND with \Draft flag
        mock_imap_client.append.assert_called_once()
        assert mock_imap_client.append.call_args[1]["flags"] == [b"\\Draft"]
        # Then \Deleted on old UID + expunge
        mock_imap_client.add_flags.assert_called_with([42], [b"\\Deleted"])
        mock_imap_client.expunge.assert_called_once()

    def test_update_draft_namespace_retry(self, imap_client, mock_imap_client):
        mock_imap_client.append.side_effect = [Exception("no such mailbox"), b"OK"]
        imap_client.update_draft(
            uid=42, to=["a@x.com"], subject="s", body="b",
        )
        assert mock_imap_client.append.call_count == 2
        assert mock_imap_client.append.call_args_list[1][0][0] == "INBOX.Drafts"


class TestDeleteDraft:
    def test_delete_draft(self, imap_client, mock_imap_client):
        result = imap_client.delete_draft(uid=42)
        assert result["uid"] == 42
        mock_imap_client.add_flags.assert_called_with([42], [b"\\Deleted"])
        mock_imap_client.expunge.assert_called_once()


# ---------------------------------------------------------------------------
# Spam
# ---------------------------------------------------------------------------


class TestReportSpam:
    def test_report_spam_default(self, imap_client, mock_imap_client):
        result = imap_client.report_spam(uids=[1, 2], mailbox="INBOX")
        assert result["reported"] == 2
        assert result["moved_to"] == "Spam"
        mock_imap_client.add_flags.assert_called_once_with([1, 2], [b"$Junk"])
        mock_imap_client.move.assert_called_once_with([1, 2], "Spam")

    def test_report_spam_custom_folder_and_flag(self, imap_client, mock_imap_client):
        result = imap_client.report_spam(
            uids=[1], mailbox="INBOX",
            spam_folder="[Gmail]/Spam", flag="",
        )
        assert result["moved_to"] == "[Gmail]/Spam"
        # Empty flag means no add_flags call
        mock_imap_client.add_flags.assert_not_called()

    def test_report_spam_namespace_retry(self, imap_client, mock_imap_client):
        mock_imap_client.move.side_effect = [Exception("namespace"), True]
        result = imap_client.report_spam(uids=[1], mailbox="INBOX")
        assert result["moved_to"] == "INBOX.Spam"


class TestMarkNotSpam:
    def test_mark_not_spam(self, imap_client, mock_imap_client):
        result = imap_client.mark_not_spam(uids=[5])
        assert result["unspammed"] == 1
        assert result["moved_to"] == "INBOX"
        # \Junk removed, $NotJunk added
        flag_calls = mock_imap_client.remove_flags.call_args_list
        add_calls = mock_imap_client.add_flags.call_args_list
        assert any(c[0] == ([5], [b"$Junk"]) for c in flag_calls)
        assert any(c[0] == ([5], [b"$NotJunk"]) for c in add_calls)
        mock_imap_client.move.assert_called_once_with([5], "INBOX")


# ---------------------------------------------------------------------------
# Server metadata
# ---------------------------------------------------------------------------


class TestServerMetadata:
    def test_get_capabilities(self, imap_client, mock_imap_client):
        mock_imap_client.capabilities.return_value = (b"IMAP4REV1", b"IDLE", b"THREAD=REFERENCES")
        caps = imap_client.get_capabilities()
        assert "IDLE" in caps
        assert "THREAD=REFERENCES" in caps
        assert caps == sorted(caps)

    def test_get_namespace(self, imap_client, mock_imap_client):
        ns = SimpleNamespace(
            personal=[(b"INBOX.", b".")], other_users=[], shared=None
        )
        mock_imap_client.namespace.return_value = ns
        result = imap_client.get_namespace()
        assert result["personal"] == [{"prefix": "INBOX.", "delimiter": "."}]
        assert result["other_users"] == []
        assert result["shared"] == []

    def test_get_namespace_error(self, imap_client, mock_imap_client):
        mock_imap_client.namespace.side_effect = Exception("not supported")
        result = imap_client.get_namespace()
        assert "error" in result

    def test_get_quota(self, imap_client, mock_imap_client):
        Resource = SimpleNamespace
        mock_imap_client.get_quota_root.return_value = (
            [SimpleNamespace(mailbox=b"INBOX", quota_root=b"")],
            [SimpleNamespace(quota_root=b"", resource=b"STORAGE", usage=12345, limit=1000000)],
        )
        result = imap_client.get_quota()
        assert result["mailbox"] == "INBOX"
        assert result["resources"][0]["resource"] == "STORAGE"
        assert result["resources"][0]["usage"] == 12345

    def test_get_quota_unsupported(self, imap_client, mock_imap_client):
        mock_imap_client.get_quota_root.side_effect = Exception("no quota")
        result = imap_client.get_quota()
        assert "error" in result

    def test_get_server_id(self, imap_client, mock_imap_client):
        mock_imap_client.id_.return_value = {b"name": b"Dovecot", b"version": b"2.3"}
        result = imap_client.get_server_id()
        assert result["name"] == "Dovecot"
        assert result["version"] == "2.3"
