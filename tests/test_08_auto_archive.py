"""Tests for auto-archive functions (all mocked)."""

import json
import os
import tempfile
from datetime import datetime

import pytest
from imap_mcp.models import AutoArchiveSender
from tests.conftest import make_envelope


class TestAutoArchive:
    """Test auto-archive functions: get_auto_archive_list, add_auto_archive_sender, remove_auto_archive_sender, reload_auto_archive, process_auto_archive."""

    def test_get_auto_archive_list_empty(self, imap_client):
        """Test getting auto-archive sender list when empty."""
        imap_client.auto_archive_senders = []
        result = imap_client.get_auto_archive_list()
        assert isinstance(result, list)
        assert len(result) == 0

    def test_get_auto_archive_list_populated(self, imap_client):
        """Test getting auto-archive sender list with entries."""
        imap_client.auto_archive_senders = [
            AutoArchiveSender(email="a@example.com", added_at=datetime.now()),
            AutoArchiveSender(email="b@example.com", comment="noisy", added_at=datetime.now()),
        ]
        result = imap_client.get_auto_archive_list()
        assert len(result) == 2
        assert all(isinstance(s, AutoArchiveSender) for s in result)

    def test_add_auto_archive_sender(self, imap_client, tmp_path):
        """Test adding sender to auto-archive list."""
        imap_client.auto_archive_senders = []
        senders_file = str(tmp_path / "senders.json")
        imap_client.config["auto_archive"] = {"enabled": True, "senders_file": senders_file}

        result = imap_client.add_auto_archive_sender(
            email_addr="test@example.com", comment="TDD Test"
        )
        assert result is True

        senders = imap_client.get_auto_archive_list()
        found = any(s.email == "test@example.com" for s in senders)
        assert found

    def test_add_auto_archive_sender_without_comment(self, imap_client, tmp_path):
        """Test adding sender without comment."""
        imap_client.auto_archive_senders = []
        senders_file = str(tmp_path / "senders.json")
        imap_client.config["auto_archive"] = {"enabled": True, "senders_file": senders_file}

        result = imap_client.add_auto_archive_sender(email_addr="nocomment@example.com")
        assert result is True

        senders = imap_client.get_auto_archive_list()
        sender = next((s for s in senders if s.email == "nocomment@example.com"), None)
        assert sender is not None
        assert sender.comment is None

    def test_remove_auto_archive_sender(self, imap_client, tmp_path):
        """Test removing sender from auto-archive list."""
        senders_file = str(tmp_path / "senders.json")
        imap_client.config["auto_archive"] = {"enabled": True, "senders_file": senders_file}
        imap_client.auto_archive_senders = [
            AutoArchiveSender(email="remove@example.com", added_at=datetime.now()),
        ]

        result = imap_client.remove_auto_archive_sender("remove@example.com")
        assert result is True

        senders = imap_client.get_auto_archive_list()
        found = any(s.email == "remove@example.com" for s in senders)
        assert not found

    def test_remove_nonexistent_sender(self, imap_client, tmp_path):
        """Test removing non-existent sender doesn't fail."""
        senders_file = str(tmp_path / "senders.json")
        imap_client.config["auto_archive"] = {"enabled": True, "senders_file": senders_file}
        imap_client.auto_archive_senders = []

        result = imap_client.remove_auto_archive_sender("nonexistent@example.com")
        assert result is True

    def test_reload_auto_archive(self, imap_client, tmp_path):
        """Test reloading auto-archive config from file."""
        senders_file = str(tmp_path / "senders.json")
        imap_client.config["auto_archive"] = {"enabled": True, "senders_file": senders_file}

        # Write a senders file
        data = {
            "senders": [
                {"email": "reloaded@example.com", "comment": "test", "added_at": "2026-01-01T00:00:00"},
            ]
        }
        with open(senders_file, "w") as f:
            json.dump(data, f)

        result = imap_client.reload_auto_archive()
        assert result is True

        senders = imap_client.get_auto_archive_list()
        assert any(s.email == "reloaded@example.com" for s in senders)

    def test_auto_archive_persistence(self, imap_client, tmp_path):
        """Test that auto-archive list is persisted to file and can be reloaded."""
        senders_file = str(tmp_path / "senders.json")
        imap_client.config["auto_archive"] = {"enabled": True, "senders_file": senders_file}
        imap_client.auto_archive_senders = []

        # Add sender (this saves to file)
        imap_client.add_auto_archive_sender(email_addr="persist@example.com", comment="Persistence test")

        # Clear in-memory list
        imap_client.auto_archive_senders = []

        # Reload from file
        imap_client.reload_auto_archive()

        senders = imap_client.get_auto_archive_list()
        found = any(s.email == "persist@example.com" for s in senders)
        assert found

    def test_process_auto_archive_empty_list(self, imap_client, mock_imap_client):
        """Test process_auto_archive with no senders in list."""
        imap_client.auto_archive_senders = []
        result = imap_client.process_auto_archive()
        assert isinstance(result, dict)
        assert result["archived_count"] == 0
        assert "No senders" in result.get("message", "")

    def test_process_auto_archive_dry_run(self, imap_client, mock_imap_client):
        """Test process_auto_archive in dry_run mode."""
        imap_client.auto_archive_senders = [
            AutoArchiveSender(email="spammer@spam.com", added_at=datetime.now()),
        ]
        # Set up matching emails
        mock_imap_client.search.return_value = [101, 102]
        env1 = make_envelope(from_mailbox="spammer", from_host="spam.com", subject=b"Buy now!")
        env2 = make_envelope(from_mailbox="friend", from_host="good.com", subject=b"Hello")
        mock_imap_client.fetch.side_effect = None
        mock_imap_client.fetch.return_value = {
            101: {b"ENVELOPE": env1},
            102: {b"ENVELOPE": env2},
        }

        result = imap_client.process_auto_archive(dry_run=True)
        assert isinstance(result, dict)
        assert result["dry_run"] is True
        assert result["archived_count"] == 1  # Only spammer matches
        # In dry run, move should NOT be called
        mock_imap_client.move.assert_not_called()

    def test_process_auto_archive_real(self, imap_client, mock_imap_client):
        """Test process_auto_archive actually moves emails."""
        imap_client.auto_archive_senders = [
            AutoArchiveSender(email="newsletter@news.com", added_at=datetime.now()),
        ]
        mock_imap_client.search.return_value = [201]
        env = make_envelope(from_mailbox="newsletter", from_host="news.com", subject=b"Weekly digest")
        mock_imap_client.fetch.side_effect = None
        mock_imap_client.fetch.return_value = {
            201: {b"ENVELOPE": env},
        }

        result = imap_client.process_auto_archive(dry_run=False)
        assert isinstance(result, dict)
        assert result["archived_count"] == 1
        assert result["dry_run"] is False
        mock_imap_client.move.assert_called_once()

    def test_process_auto_archive_empty_inbox(self, imap_client, mock_imap_client):
        """Test process_auto_archive with empty inbox."""
        imap_client.auto_archive_senders = [
            AutoArchiveSender(email="x@y.com", added_at=datetime.now()),
        ]
        mock_imap_client.search.return_value = []
        result = imap_client.process_auto_archive()
        assert result["archived_count"] == 0
        assert "empty" in result["message"].lower()

    def test_process_auto_archive_domain_match(self, imap_client, mock_imap_client):
        """Test process_auto_archive with domain-level matching."""
        imap_client.auto_archive_senders = [
            AutoArchiveSender(email="@spam.com", added_at=datetime.now()),
        ]
        mock_imap_client.search.return_value = [301]
        env = make_envelope(from_mailbox="anyone", from_host="spam.com", subject=b"Spam")
        mock_imap_client.fetch.side_effect = None
        mock_imap_client.fetch.return_value = {
            301: {b"ENVELOPE": env},
        }

        result = imap_client.process_auto_archive(dry_run=True)
        assert result["archived_count"] == 1
