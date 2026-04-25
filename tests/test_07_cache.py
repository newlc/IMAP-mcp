"""Tests for cache and watch functions (all mocked)."""

import pytest
from unittest.mock import MagicMock, patch
from tests.conftest import make_fetch_response


class TestCacheAndWatch:
    """Test cache functions: get_cached_overview, refresh_cache, start_watch, stop_watch, idle_watch, get_cache_stats."""

    def test_get_cached_overview_no_watcher(self, imap_client, mock_imap_client):
        """Test getting cached overview when no watcher is running (manual fetch)."""
        imap_client.watcher = None
        imap_client.watching = False
        result = imap_client.get_cached_overview()
        assert isinstance(result, dict)
        # Should have inbox key at minimum
        assert "inbox" in result

    def test_get_cached_overview_single(self, imap_client, mock_imap_client):
        """Test getting cached overview for single mailbox."""
        imap_client.watcher = None
        imap_client.watching = False
        result = imap_client.get_cached_overview(mailbox="inbox")
        assert isinstance(result, dict)
        assert "inbox" in result

    def test_get_cached_overview_with_limit(self, imap_client, mock_imap_client):
        """Test cached overview respects limit."""
        imap_client.watcher = None
        imap_client.watching = False
        result = imap_client.get_cached_overview(mailbox="inbox", limit=5)
        assert isinstance(result, dict)
        if "inbox" in result and "emails" in result["inbox"]:
            assert len(result["inbox"]["emails"]) <= 5

    def test_get_cached_overview_with_watcher(self, imap_client):
        """Test getting cached overview when watcher is running."""
        mock_watcher = MagicMock()
        mock_watcher.running = True
        mock_watcher.get_cache.return_value = {
            "inbox": {
                "emails": [{"uid": 1, "subject": "Test"}],
                "total": 100,
                "unread": 5,
            }
        }
        imap_client.watcher = mock_watcher
        imap_client.watching = True

        result = imap_client.get_cached_overview()
        assert isinstance(result, dict)
        assert "inbox" in result
        assert result["inbox"]["total"] == 100

    def test_get_cached_overview_unknown_mailbox(self, imap_client, mock_imap_client):
        """Test getting cached overview for unknown mailbox returns empty dict."""
        imap_client.watcher = None
        result = imap_client.get_cached_overview(mailbox="nonexistent")
        assert result == {}

    def test_refresh_cache_no_watcher(self, imap_client, mock_imap_client):
        """Test refreshing cache clears in-memory cache."""
        imap_client.watcher = None
        imap_client.watching = False
        imap_client.cache["test_key"] = "test_value"
        result = imap_client.refresh_cache()
        assert result is True

    def test_refresh_cache_with_watcher(self, imap_client):
        """Test refreshing cache calls watcher refresh."""
        mock_watcher = MagicMock()
        mock_watcher.running = True
        imap_client.watcher = mock_watcher
        imap_client.watching = True

        result = imap_client.refresh_cache()
        assert result is True
        mock_watcher.refresh.assert_called_once()

    def test_start_watch(self, imap_client):
        """Test starting IDLE watch."""
        mock_watcher = MagicMock()
        imap_client.watcher = mock_watcher
        result = imap_client.start_watch()
        assert result is True
        assert imap_client.watching is True
        mock_watcher.start.assert_called_once()

    def test_stop_watch(self, imap_client):
        """Test stopping IDLE watch."""
        mock_watcher = MagicMock()
        imap_client.watcher = mock_watcher
        imap_client.watching = True

        result = imap_client.stop_watch()
        assert result is True
        assert imap_client.watching is False
        mock_watcher.stop.assert_called_once()

    def test_idle_watch_short_timeout(self, imap_client, mock_imap_client):
        """Test IDLE watch with short timeout."""
        mock_imap_client.idle_check.return_value = []
        result = imap_client.idle_watch(mailbox="INBOX", timeout=2)
        assert isinstance(result, dict)
        assert result["mailbox"] == "INBOX"
        assert "responses" in result
        mock_imap_client.idle.assert_called_once()
        mock_imap_client.idle_check.assert_called_once_with(timeout=2)
        mock_imap_client.idle_done.assert_called_once()

    def test_idle_watch_with_responses(self, imap_client, mock_imap_client):
        """Test IDLE watch returns responses."""
        mock_imap_client.idle_check.return_value = [(1, b"EXISTS"), (2, b"RECENT")]
        result = imap_client.idle_watch(mailbox="INBOX", timeout=5)
        assert len(result["responses"]) == 2

    def test_get_cache_stats_no_cache(self, imap_client):
        """Test get_cache_stats when cache not initialized."""
        imap_client.email_cache = None
        result = imap_client.get_cache_stats()
        assert "error" in result

    def test_get_cache_stats_with_cache(self, imap_client, tmp_cache_db):
        """Test get_cache_stats with initialized cache."""
        from imap_mcp.cache import EmailCache

        with patch("imap_mcp.cache.keyring"):
            cache = EmailCache(tmp_cache_db, encrypted=False)
            imap_client.email_cache = cache
            result = imap_client.get_cache_stats()
            assert "emails_cached" in result
            assert "db_path" in result
            cache.close()
