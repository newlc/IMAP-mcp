"""Tests for _to_imap_date helper method."""

import pytest
from imap_mcp.imap_client import ImapClientWrapper


class TestToImapDate:
    """Test the _to_imap_date static method."""

    def test_iso_date_conversion(self):
        """Test converting ISO date to IMAP format."""
        assert ImapClientWrapper._to_imap_date("2026-04-24") == "24-Apr-2026"

    def test_january(self):
        assert ImapClientWrapper._to_imap_date("2024-01-15") == "15-Jan-2024"

    def test_february(self):
        assert ImapClientWrapper._to_imap_date("2024-02-28") == "28-Feb-2024"

    def test_december(self):
        assert ImapClientWrapper._to_imap_date("2025-12-31") == "31-Dec-2025"

    def test_first_day_of_year(self):
        assert ImapClientWrapper._to_imap_date("2026-01-01") == "01-Jan-2026"

    def test_empty_string_passthrough(self):
        """Test that empty string is returned as-is."""
        assert ImapClientWrapper._to_imap_date("") == ""

    def test_none_passthrough(self):
        """Test that None is returned as-is."""
        assert ImapClientWrapper._to_imap_date(None) is None

    def test_already_imap_format_passthrough(self):
        """Test that already-formatted IMAP date is returned as-is."""
        assert ImapClientWrapper._to_imap_date("24-Apr-2026") == "24-Apr-2026"

    def test_invalid_format_passthrough(self):
        """Test that invalid date string is returned as-is."""
        assert ImapClientWrapper._to_imap_date("not-a-date") == "not-a-date"

    def test_partial_date_passthrough(self):
        """Test that partial date is returned as-is."""
        assert ImapClientWrapper._to_imap_date("2026-04") == "2026-04"

    def test_all_months(self):
        """Test conversion for all 12 months."""
        expected = [
            ("2026-01-01", "01-Jan-2026"),
            ("2026-02-01", "01-Feb-2026"),
            ("2026-03-01", "01-Mar-2026"),
            ("2026-04-01", "01-Apr-2026"),
            ("2026-05-01", "01-May-2026"),
            ("2026-06-01", "01-Jun-2026"),
            ("2026-07-01", "01-Jul-2026"),
            ("2026-08-01", "01-Aug-2026"),
            ("2026-09-01", "01-Sep-2026"),
            ("2026-10-01", "01-Oct-2026"),
            ("2026-11-01", "01-Nov-2026"),
            ("2026-12-01", "01-Dec-2026"),
        ]
        for iso, imap in expected:
            assert ImapClientWrapper._to_imap_date(iso) == imap
