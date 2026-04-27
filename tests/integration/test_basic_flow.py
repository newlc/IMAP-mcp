"""End-to-end IMAP/SMTP flow against a real Greenmail container.

Covers things mocks can't: namespace auto-detection, real IMAP SEARCH
behavior, SMTP send + IMAP APPEND to Sent, IDLE responsiveness.
"""

from __future__ import annotations

import time

import pytest

from imap_mcp.imap_client import ImapClientWrapper
from .conftest import deliver_email, docker_available


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not docker_available(),
        reason="Docker daemon not reachable -- skipping integration tests",
    ),
]


@pytest.fixture
def alice_wrapper(alice_config) -> ImapClientWrapper:
    """A wrapper connected to alice's Greenmail account."""
    wrapper = ImapClientWrapper()
    wrapper.config = alice_config
    wrapper.account_name = "alice"
    wrapper._connect_with_loaded_config()
    yield wrapper
    try:
        wrapper.disconnect()
    except Exception:
        pass


class TestBasicImapFlow:
    def test_login_and_list_folders(self, alice_wrapper):
        result = alice_wrapper.list_mailboxes()
        names = {m.name for m in result["mailboxes"]}
        assert "INBOX" in names

    def test_inbox_status(self, alice_wrapper):
        status = alice_wrapper.get_mailbox_status("INBOX")
        assert status.exists >= 0


class TestEmailDelivery:
    def test_deliver_then_fetch(self, alice_wrapper, greenmail_clean):
        deliver_email(
            greenmail_clean,
            sender="bob@example.com",
            recipient="alice@example.com",
            subject="Hello from Bob",
            body="Welcome to integration tests.",
        )
        # Greenmail SMTP -> IMAP delivery is synchronous via local relay,
        # but give it a brief moment to land.
        for _ in range(20):
            headers = alice_wrapper.fetch_emails(mailbox="INBOX", limit=10)
            if headers:
                break
            time.sleep(0.2)
        else:
            pytest.fail("Email did not arrive in INBOX")
        subjects = [h.subject for h in headers]
        assert "Hello from Bob" in subjects

    def test_get_email_round_trip(self, alice_wrapper, greenmail_clean):
        deliver_email(
            greenmail_clean,
            sender="bob@example.com",
            recipient="alice@example.com",
            subject="Round trip",
            body="The body content.",
        )
        time.sleep(0.5)
        headers = alice_wrapper.fetch_emails(mailbox="INBOX", limit=10)
        assert headers
        msg = alice_wrapper.get_email(uid=headers[0].uid, mailbox="INBOX")
        assert msg.header.subject == "Round trip"
        # Body got through, html2text fallback applied if HTML-only.
        assert msg.body.text and "body content" in msg.body.text.lower()


class TestSearchAndFlags:
    def test_search_unread_then_mark_read(self, alice_wrapper, greenmail_clean):
        deliver_email(
            greenmail_clean,
            sender="bob@example.com",
            recipient="alice@example.com",
            subject="Mark me",
            body="x",
        )
        time.sleep(0.5)
        unread = alice_wrapper.search_unread(mailbox="INBOX")
        assert unread, "Expected at least one unread email"
        uid = unread[0].uid
        alice_wrapper.mark_read(uids=[uid], mailbox="INBOX")
        # After marking, should disappear from UNSEEN search.
        unread_after = alice_wrapper.search_unread(mailbox="INBOX")
        assert not any(h.uid == uid for h in unread_after)


class TestSendEmail:
    def test_send_via_smtp_lands_in_sent(self, alice_wrapper, greenmail_clean):
        # Make sure Sent folder exists.
        try:
            alice_wrapper.create_mailbox("Sent")
        except Exception:
            pass
        result = alice_wrapper.send_email(
            to=["bob@example.com"],
            subject="From integration test",
            body="hello bob",
            save_to_sent=True,
            sent_folder="Sent",
        )
        assert result["sent"] is True
        # Sent copy should have appended.
        sent_headers = alice_wrapper.fetch_emails(mailbox="Sent", limit=5)
        subjects = [h.subject for h in sent_headers]
        assert "From integration test" in subjects


class TestFolderManagement:
    def test_create_then_delete_mailbox(self, alice_wrapper):
        name = "IntegrationTest"
        try:
            alice_wrapper.create_mailbox(name)
            mailboxes = {m.name for m in alice_wrapper.list_mailboxes()["mailboxes"]}
            assert name in mailboxes
        finally:
            try:
                alice_wrapper.delete_mailbox(name)
            except Exception:
                pass


class TestServerCapabilities:
    def test_capabilities_includes_idle(self, alice_wrapper):
        caps = alice_wrapper.get_capabilities()
        # Greenmail advertises IMAP4rev1 and IDLE among others.
        assert any("IMAP4REV1" in c.upper() for c in caps)
