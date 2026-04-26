"""Pytest configuration and fixtures for IMAP MCP tests.

All IMAP interactions are mocked -- no network connection required.
"""

import os
import sys
import tempfile
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from imap_mcp.imap_client import ImapClientWrapper
from imap_mcp.models import EmailAddress


# ---------------------------------------------------------------------------
# Helpers to build realistic IMAP response objects
# ---------------------------------------------------------------------------


def make_address(name: str, mailbox: str, host: str):
    """Create a mock IMAP ENVELOPE address object."""
    addr = SimpleNamespace()
    addr.name = name.encode() if name else None
    addr.mailbox = mailbox.encode() if mailbox else None
    addr.host = host.encode() if host else None
    return addr


def make_envelope(
    date=None,
    subject=b"Test Subject",
    from_name="Sender Name",
    from_mailbox="sender",
    from_host="example.com",
    to_list=None,
    cc_list=None,
    message_id=b"<msg-001@example.com>",
):
    """Create a mock IMAP ENVELOPE object."""
    env = SimpleNamespace()
    env.date = date or datetime(2026, 4, 20, 10, 30, 0)
    env.subject = subject
    env.from_ = [make_address(from_name, from_mailbox, from_host)]
    env.to = to_list or [make_address("Recipient", "recipient", "example.com")]
    env.cc = cc_list
    env.message_id = message_id
    return env


def make_fetch_response(
    uids,
    envelope_factory=None,
    include_body=False,
    flags=(b"\\Seen",),
    size=4096,
):
    """Build a dict[uid, data] mimicking IMAPClient.fetch() output.

    Args:
        uids: list of UID ints.
        envelope_factory: callable(uid) -> envelope; defaults to make_envelope.
        include_body: if True, includes a simple RFC822 body.
        flags: tuple of flag bytes.
        size: RFC822.SIZE value.
    """
    if envelope_factory is None:
        envelope_factory = lambda uid: make_envelope(
            subject=f"Subject {uid}".encode(),
            message_id=f"<msg-{uid}@example.com>".encode(),
        )

    result = {}
    for uid in uids:
        data = {
            b"ENVELOPE": envelope_factory(uid),
            b"FLAGS": flags,
            b"RFC822.SIZE": size,
        }
        if include_body:
            data[b"BODY[]"] = (
                b"From: sender@example.com\r\n"
                b"To: recipient@example.com\r\n"
                b"Subject: Test\r\n"
                b"Content-Type: text/plain; charset=utf-8\r\n"
                b"\r\n"
                b"Hello, this is the body.\r\n"
            )
        result[uid] = data
    return result


# Multipart body with attachment for attachment tests
MULTIPART_BODY = (
    b"MIME-Version: 1.0\r\n"
    b"From: sender@example.com\r\n"
    b"To: recipient@example.com\r\n"
    b"Subject: Test with attachment\r\n"
    b'Content-Type: multipart/mixed; boundary="boundary123"\r\n'
    b"\r\n"
    b"--boundary123\r\n"
    b"Content-Type: text/plain; charset=utf-8\r\n"
    b"\r\n"
    b"Body text here.\r\n"
    b"--boundary123\r\n"
    b"Content-Type: application/pdf\r\n"
    b'Content-Disposition: attachment; filename="report.pdf"\r\n'
    b"Content-Transfer-Encoding: base64\r\n"
    b"\r\n"
    b"JVBER\r\n"
    b"--boundary123--\r\n"
)


# Standard select_folder return dict
SELECT_FOLDER_RESPONSE = {
    b"EXISTS": 150,
    b"RECENT": 3,
    b"UNSEEN": 10,
    b"UIDNEXT": 44832,
    b"UIDVALIDITY": 1,
}

# Standard folder_status return dict
FOLDER_STATUS_RESPONSE = {
    b"MESSAGES": 150,
    b"RECENT": 3,
    b"UNSEEN": 10,
    b"UIDNEXT": 44832,
    b"UIDVALIDITY": 1,
}

# Standard list_folders return value
LIST_FOLDERS_RESPONSE = [
    ((b"\\HasNoChildren",), "/", "INBOX"),
    ((b"\\HasNoChildren",), "/", "Drafts"),
    ((b"\\HasNoChildren",), "/", "Sent"),
    ((b"\\HasNoChildren", b"\\Trash"), "/", "Trash"),
    ((b"\\HasNoChildren",), "/", "Archive"),
    ((b"\\HasNoChildren",), "/", "next"),
    ((b"\\HasNoChildren",), "/", "waiting"),
    ((b"\\HasNoChildren",), "/", "someday"),
]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_imap_client():
    """Return a MagicMock that behaves like imapclient.IMAPClient."""
    client = MagicMock()
    client.login.return_value = b"OK"
    client.logout.return_value = b"OK"
    client.select_folder.return_value = dict(SELECT_FOLDER_RESPONSE)
    client.folder_status.return_value = dict(FOLDER_STATUS_RESPONSE)
    client.list_folders.return_value = list(LIST_FOLDERS_RESPONSE)
    client.search.return_value = [101, 102, 103, 104, 105]
    client.fetch.side_effect = lambda uids, fields: make_fetch_response(uids)
    client.create_folder.return_value = True
    client.delete_folder.return_value = True
    client.move.return_value = True
    client.copy.return_value = True
    client.add_flags.return_value = True
    client.remove_flags.return_value = True
    client.append.return_value = True
    client.idle.return_value = None
    client.idle_check.return_value = []
    client.idle_done.return_value = (b"OK", [])
    return client


@pytest.fixture
def imap_wrapper(mock_imap_client):
    """Return an ImapClientWrapper with a mocked IMAPClient already set up.

    * ``self.client`` is the mock_imap_client.
    * ``self.config`` has minimal valid structure.
    * ``self.email_cache`` is None (no persistent cache unless a test sets it).
    * ``keyring`` module is patched to avoid touching the real keyring.
    """
    wrapper = ImapClientWrapper()
    wrapper.client = mock_imap_client
    wrapper.current_mailbox = "INBOX"
    wrapper.config = {
        "imap": {"host": "imap.example.com", "port": 993, "secure": True},
        "smtp": {"host": "smtp.example.com", "port": 587, "secure": False, "starttls": True},
        "credentials": {"username": "user@example.com", "password": "secret"},
        "folders": {
            "inbox": "INBOX",
            "next": "next",
            "waiting": "waiting",
            "someday": "someday",
            "archive": "Archive",
            "sent": "Sent",
            "trash": "Trash",
        },
        "auto_archive": {"enabled": False},
        "cache": {"enabled": False, "db_path": "/tmp/test-imap-cache.db", "encrypt": False},
        "user": {
            "name": "Test User",
            "email": "user@example.com",
            "signature": {
                "enabled": True,
                "text": "\n--\nTest User",
                "html": "<br>--<br>Test User",
            },
        },
    }
    return wrapper


@pytest.fixture
def imap_client(imap_wrapper):
    """Alias kept for backward compatibility with existing test files."""
    return imap_wrapper


@pytest.fixture
def fresh_client(mock_imap_client):
    """A fresh ImapClientWrapper without any client attached (for connection tests)."""
    return ImapClientWrapper()


@pytest.fixture
def tmp_cache_db():
    """Return a temporary file path for a test SQLite cache database.

    The file is removed after the test.
    """
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)  # EmailCache will create it
    yield path
    # Cleanup
    for p in (path, path + ".enc", path + ".enc.tmp"):
        try:
            os.unlink(p)
        except FileNotFoundError:
            pass
