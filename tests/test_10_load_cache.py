"""Tests for load_cache with all 4 modes: recent, new, older, range."""

import pytest
from unittest.mock import patch, MagicMock

from imap_mcp.cache import EmailCache
from tests.conftest import make_fetch_response, make_envelope, FOLDER_STATUS_RESPONSE


def _setup_cache_on_wrapper(wrapper, tmp_cache_db):
    """Initialize a real (unencrypted) EmailCache on the wrapper."""
    cache = EmailCache(tmp_cache_db, encrypted=False)
    wrapper.email_cache = cache
    return cache


class TestLoadCache:
    """Test load_cache method with all four modes."""

    def test_load_cache_no_cache_raises(self, imap_client, mock_imap_client):
        """Test that load_cache raises when cache is not initialized."""
        imap_client.email_cache = None
        with pytest.raises(RuntimeError, match="Cache not initialized"):
            imap_client.load_cache()

    # --- mode: recent ---

    def test_load_cache_recent(self, imap_client, mock_imap_client, tmp_cache_db):
        """Test load_cache in 'recent' mode fetches newest N emails."""
        cache = _setup_cache_on_wrapper(imap_client, tmp_cache_db)

        mock_imap_client.search.return_value = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
        mock_imap_client.fetch.side_effect = lambda uids, fields: make_fetch_response(
            uids, include_body=True
        )

        result = imap_client.load_cache(mailbox="INBOX", mode="recent", count=5)
        assert result["loaded"] == 5
        assert result["mode"] == "recent"
        assert result["mailbox"] == "INBOX"
        cache.close()

    def test_load_cache_recent_skips_cached(self, imap_client, mock_imap_client, tmp_cache_db):
        """Test load_cache in 'recent' mode skips already-cached UIDs."""
        cache = _setup_cache_on_wrapper(imap_client, tmp_cache_db)
        # Pre-populate cache with uid 10, 9
        from tests.test_09_persistent_cache import _sample_header, _sample_body
        cache.store_email("INBOX", 10, _sample_header(10), _sample_body())
        cache.store_email("INBOX", 9, _sample_header(9), _sample_body())

        mock_imap_client.search.return_value = [6, 7, 8, 9, 10]
        mock_imap_client.fetch.side_effect = lambda uids, fields: make_fetch_response(
            uids, include_body=True
        )

        result = imap_client.load_cache(mailbox="INBOX", mode="recent", count=5)
        assert result["loaded"] == 3  # 6, 7, 8
        assert result["already_cached"] == 2
        cache.close()

    def test_load_cache_recent_nothing_to_load(self, imap_client, mock_imap_client, tmp_cache_db):
        """Test load_cache returns message when nothing to load."""
        cache = _setup_cache_on_wrapper(imap_client, tmp_cache_db)
        from tests.test_09_persistent_cache import _sample_header, _sample_body
        for uid in [1, 2, 3]:
            cache.store_email("INBOX", uid, _sample_header(uid), _sample_body())

        mock_imap_client.search.return_value = [1, 2, 3]

        result = imap_client.load_cache(mailbox="INBOX", mode="recent", count=3)
        assert result["loaded"] == 0
        assert "Nothing new" in result["message"]
        cache.close()

    # --- mode: new ---

    def test_load_cache_new(self, imap_client, mock_imap_client, tmp_cache_db):
        """Test load_cache in 'new' mode fetches emails newer than max cached UID."""
        cache = _setup_cache_on_wrapper(imap_client, tmp_cache_db)
        from tests.test_09_persistent_cache import _sample_header, _sample_body
        cache.store_email("INBOX", 50, _sample_header(50), _sample_body())

        mock_imap_client.search.return_value = [51, 52, 53]
        mock_imap_client.fetch.side_effect = lambda uids, fields: make_fetch_response(
            uids, include_body=True
        )

        result = imap_client.load_cache(mailbox="INBOX", mode="new")
        assert result["loaded"] == 3
        assert result["mode"] == "new"
        cache.close()

    def test_load_cache_new_no_cached_falls_back_to_recent(self, imap_client, mock_imap_client, tmp_cache_db):
        """Test load_cache 'new' with no cached emails falls back to recent."""
        cache = _setup_cache_on_wrapper(imap_client, tmp_cache_db)

        mock_imap_client.search.return_value = [1, 2, 3, 4, 5]
        mock_imap_client.fetch.side_effect = lambda uids, fields: make_fetch_response(
            uids, include_body=True
        )

        result = imap_client.load_cache(mailbox="INBOX", mode="new", count=3)
        assert result["loaded"] == 3
        cache.close()

    def test_load_cache_new_filters_max_uid(self, imap_client, mock_imap_client, tmp_cache_db):
        """Test that 'new' mode filters out max_uid itself (IMAP range is inclusive)."""
        cache = _setup_cache_on_wrapper(imap_client, tmp_cache_db)
        from tests.test_09_persistent_cache import _sample_header, _sample_body
        cache.store_email("INBOX", 100, _sample_header(100), _sample_body())

        # IMAP returns max_uid in range "101:*" but we may also get 100
        mock_imap_client.search.return_value = [100, 101, 102]
        mock_imap_client.fetch.side_effect = lambda uids, fields: make_fetch_response(
            uids, include_body=True
        )

        result = imap_client.load_cache(mailbox="INBOX", mode="new")
        assert result["loaded"] == 2  # 101, 102 only
        cache.close()

    # --- mode: older ---

    def test_load_cache_older(self, imap_client, mock_imap_client, tmp_cache_db):
        """Test load_cache in 'older' mode fetches emails older than min cached UID."""
        cache = _setup_cache_on_wrapper(imap_client, tmp_cache_db)
        from tests.test_09_persistent_cache import _sample_header, _sample_body
        cache.store_email("INBOX", 50, _sample_header(50), _sample_body())

        mock_imap_client.search.return_value = [40, 41, 42, 43, 44, 45, 46, 47, 48, 49]
        mock_imap_client.fetch.side_effect = lambda uids, fields: make_fetch_response(
            uids, include_body=True
        )

        result = imap_client.load_cache(mailbox="INBOX", mode="older", count=5)
        assert result["loaded"] == 5
        assert result["mode"] == "older"
        cache.close()

    def test_load_cache_older_nothing_older(self, imap_client, mock_imap_client, tmp_cache_db):
        """Test load_cache 'older' when min_uid is 1."""
        cache = _setup_cache_on_wrapper(imap_client, tmp_cache_db)
        from tests.test_09_persistent_cache import _sample_header, _sample_body
        cache.store_email("INBOX", 1, _sample_header(1), _sample_body())

        result = imap_client.load_cache(mailbox="INBOX", mode="older", count=5)
        assert result["loaded"] == 0
        assert "Nothing new" in result["message"]
        cache.close()

    def test_load_cache_older_empty_cache(self, imap_client, mock_imap_client, tmp_cache_db):
        """Test load_cache 'older' with empty cache returns nothing."""
        cache = _setup_cache_on_wrapper(imap_client, tmp_cache_db)

        result = imap_client.load_cache(mailbox="INBOX", mode="older", count=5)
        assert result["loaded"] == 0
        cache.close()

    # --- mode: range ---

    def test_load_cache_range(self, imap_client, mock_imap_client, tmp_cache_db):
        """Test load_cache in 'range' mode fetches emails in date range."""
        cache = _setup_cache_on_wrapper(imap_client, tmp_cache_db)

        mock_imap_client.search.return_value = [10, 11, 12]
        mock_imap_client.fetch.side_effect = lambda uids, fields: make_fetch_response(
            uids, include_body=True
        )

        result = imap_client.load_cache(
            mailbox="INBOX", mode="range",
            since="2026-01-01", before="2026-04-30",
        )
        assert result["loaded"] == 3
        assert result["mode"] == "range"

        # Verify search was called with date criteria
        call_args = mock_imap_client.search.call_args[0][0]
        assert "SINCE" in call_args
        assert "BEFORE" in call_args
        cache.close()

    def test_load_cache_range_no_dates(self, imap_client, mock_imap_client, tmp_cache_db):
        """Test load_cache 'range' with no dates uses ALL."""
        cache = _setup_cache_on_wrapper(imap_client, tmp_cache_db)

        mock_imap_client.search.return_value = [1, 2]
        mock_imap_client.fetch.side_effect = lambda uids, fields: make_fetch_response(
            uids, include_body=True
        )

        result = imap_client.load_cache(mailbox="INBOX", mode="range")
        mock_imap_client.search.assert_called_with(["ALL"])
        cache.close()

    # --- unknown mode ---

    def test_load_cache_unknown_mode(self, imap_client, mock_imap_client, tmp_cache_db):
        """Test load_cache with unknown mode returns error."""
        cache = _setup_cache_on_wrapper(imap_client, tmp_cache_db)
        result = imap_client.load_cache(mailbox="INBOX", mode="invalid_mode")
        assert "error" in result
        assert "Unknown mode" in result["error"]
        cache.close()

    # --- error handling ---

    def test_load_cache_fetch_error(self, imap_client, mock_imap_client, tmp_cache_db):
        """Test load_cache handles fetch errors gracefully."""
        cache = _setup_cache_on_wrapper(imap_client, tmp_cache_db)

        mock_imap_client.search.return_value = [1, 2, 3]
        mock_imap_client.fetch.side_effect = Exception("Connection timeout")

        result = imap_client.load_cache(mailbox="INBOX", mode="recent", count=3)
        assert result["errors"] == 3
        assert result["loaded"] == 0
        cache.close()
