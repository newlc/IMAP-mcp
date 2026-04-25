"""Tests for _parse_status_value with int, bytes, and list inputs."""

import pytest
from imap_mcp.imap_client import ImapClientWrapper


class TestParseStatusValue:
    """Test the _parse_status_value static method.

    Some IMAP servers return values as ints, some as bytes, and some
    as lists containing a single bytes element (e.g. [b'44831']).
    """

    def test_int_input(self):
        """Test parsing int value."""
        assert ImapClientWrapper._parse_status_value(150) == 150

    def test_int_zero(self):
        assert ImapClientWrapper._parse_status_value(0) == 0

    def test_bytes_input(self):
        """Test parsing bytes value like b'150'."""
        assert ImapClientWrapper._parse_status_value(b"150") == 150

    def test_bytes_zero(self):
        assert ImapClientWrapper._parse_status_value(b"0") == 0

    def test_list_with_bytes(self):
        """Test parsing list containing bytes, e.g. [b'44831']."""
        assert ImapClientWrapper._parse_status_value([b"44831"]) == 44831

    def test_list_with_single_int(self):
        """Test parsing list containing a single int."""
        assert ImapClientWrapper._parse_status_value([100]) == 100

    def test_tuple_with_bytes(self):
        """Test parsing tuple containing bytes."""
        assert ImapClientWrapper._parse_status_value((b"999",)) == 999

    def test_string_input(self):
        """Test parsing string value."""
        assert ImapClientWrapper._parse_status_value("42") == 42

    def test_large_value(self):
        """Test parsing large UID value."""
        assert ImapClientWrapper._parse_status_value(b"4294967295") == 4294967295

    def test_list_of_bytes_large(self):
        """Test parsing list with large bytes value."""
        assert ImapClientWrapper._parse_status_value([b"4294967295"]) == 4294967295
