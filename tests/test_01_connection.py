"""Tests for connection functions (all mocked, no network)."""

import pytest
from unittest.mock import MagicMock, patch

from imap_mcp.imap_client import ImapClientWrapper


class TestConnection:
    """Test connection functions: connect, authenticate, disconnect, auto_connect."""

    @patch("imap_mcp.imap_client.IMAPClient")
    def test_connect(self, mock_imapclient_cls, fresh_client):
        """Test IMAP connection creates an IMAPClient."""
        mock_instance = MagicMock()
        mock_imapclient_cls.return_value = mock_instance

        result = fresh_client.connect(host="imap.example.com", port=993, secure=True)
        assert result is True
        assert fresh_client.client is mock_instance
        mock_imapclient_cls.assert_called_once_with("imap.example.com", port=993, ssl=True)

    @patch("imap_mcp.imap_client.IMAPClient")
    def test_authenticate(self, mock_imapclient_cls, fresh_client):
        """Test IMAP authentication calls login."""
        mock_instance = MagicMock()
        mock_imapclient_cls.return_value = mock_instance

        fresh_client.connect(host="imap.example.com")
        result = fresh_client.authenticate(username="user@example.com", password="pass")
        assert result is True
        mock_instance.login.assert_called_once_with("user@example.com", "pass")

    def test_authenticate_without_connect_raises(self, fresh_client):
        """Test that authenticate fails when not connected."""
        with pytest.raises(RuntimeError, match="Not connected"):
            fresh_client.authenticate("user", "pass")

    def test_disconnect(self, imap_client, mock_imap_client):
        """Test IMAP disconnect."""
        result = imap_client.disconnect()
        assert result is True
        assert imap_client.client is None
        mock_imap_client.logout.assert_called_once()

    def test_disconnect_when_not_connected(self, fresh_client):
        """Test disconnect when already disconnected is safe."""
        result = fresh_client.disconnect()
        assert result is True

    @patch("imap_mcp.imap_client.IMAPClient")
    @patch("imap_mcp.imap_client.get_watcher")
    @patch("imap_mcp.imap_client.keyring")
    def test_auto_connect(self, mock_keyring, mock_get_watcher, mock_imapclient_cls, tmp_path):
        """Test auto_connect with a config file."""
        import json

        config = {
            "imap": {"host": "imap.example.com", "port": 993, "secure": True},
            "credentials": {"username": "user@example.com", "password": "secret"},
            "auto_archive": {"enabled": False},
            "cache": {"enabled": False, "db_path": str(tmp_path / "cache.db"), "encrypt": False},
        }
        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps(config))

        mock_instance = MagicMock()
        mock_imapclient_cls.return_value = mock_instance

        mock_watcher = MagicMock()
        mock_get_watcher.return_value = mock_watcher

        client = ImapClientWrapper()
        result = client.auto_connect(str(config_path))
        assert result is True
        assert client.client is mock_instance
        mock_instance.login.assert_called_once_with("user@example.com", "secret")
        client.disconnect()

    def test_operations_fail_without_connection(self, fresh_client):
        """Test that operations fail without authentication."""
        with pytest.raises(RuntimeError, match="Not connected"):
            fresh_client.list_mailboxes()

    def test_operations_fail_without_connection_select(self, fresh_client):
        """Test that select_mailbox fails without connection."""
        with pytest.raises(RuntimeError, match="Not connected"):
            fresh_client.select_mailbox("INBOX")
