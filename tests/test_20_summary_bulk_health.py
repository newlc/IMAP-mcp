"""Tests for the new wrapper-level features:
get_email_summary, bulk_action, get_email_body_safe, get_calendar_invites,
and health_check."""

from datetime import datetime
from unittest.mock import MagicMock

import pytest

from imap_mcp import server as srv
from tests.conftest import make_envelope, make_fetch_response
from tests.test_19_mail_utils import EMAIL_WITH_INLINE_IMAGE, EMAIL_WITH_INVITE


# ---------------------------------------------------------------------------
# get_email_summary
# ---------------------------------------------------------------------------


class TestEmailSummary:
    def test_summary_for_uncached_emails(self, imap_client, mock_imap_client):
        # No cache: should fetch envelope+flags+size+bodystructure+text in one go.
        body_bytes = (
            b"From: alice@example.com\r\n"
            b"Subject: Hello\r\n"
            b"\r\n"
            b"This is the plain-text body that the agent will see in the snippet."
        )
        def fake_fetch(uids, fields):
            assert "BODY.PEEK[TEXT]" in fields or any("BODY.PEEK[TEXT]" in str(f) for f in fields)
            out = {}
            for u in uids:
                out[u] = {
                    b"ENVELOPE": make_envelope(
                        from_mailbox="alice", from_host="example.com",
                        subject=b"Hello",
                    ),
                    b"FLAGS": (b"\\Seen",),
                    b"RFC822.SIZE": 100,
                    b"BODYSTRUCTURE": ("text", "plain", None, None, None,
                                      "7bit", 80, 5, None, None, None, None),
                    b"BODY[TEXT]": body_bytes.split(b"\r\n\r\n")[1],
                }
            return out
        mock_imap_client.fetch.side_effect = fake_fetch

        summaries = imap_client.get_email_summary(uids=[101, 102], mailbox="INBOX")
        assert len(summaries) == 2
        s = summaries[0]
        assert s["sender"] == "alice@example.com"
        assert s["subject"] == "Hello"
        assert s["unread"] is False
        assert "plain-text body" in s["snippet"]

    def test_summary_truncates_snippet(self, imap_client, mock_imap_client):
        long_body = b"x" * 1000
        def fake_fetch(uids, fields):
            return {
                u: {
                    b"ENVELOPE": make_envelope(),
                    b"FLAGS": (),
                    b"RFC822.SIZE": 1000,
                    b"BODYSTRUCTURE": None,
                    b"BODY[TEXT]": long_body,
                }
                for u in uids
            }
        mock_imap_client.fetch.side_effect = fake_fetch
        result = imap_client.get_email_summary(uids=[1], body_chars=50)
        assert len(result[0]["snippet"]) <= 51 + 1  # 50 + ellipsis

    def test_summary_preserves_input_order(self, imap_client, mock_imap_client):
        def fake_fetch(uids, fields):
            return {
                u: {
                    b"ENVELOPE": make_envelope(subject=f"Subj {u}".encode()),
                    b"FLAGS": (),
                    b"RFC822.SIZE": 0,
                    b"BODYSTRUCTURE": None,
                    b"BODY[TEXT]": b"",
                }
                for u in uids
            }
        mock_imap_client.fetch.side_effect = fake_fetch
        result = imap_client.get_email_summary(uids=[300, 100, 200])
        assert [s["uid"] for s in result] == [300, 100, 200]


# ---------------------------------------------------------------------------
# bulk_action
# ---------------------------------------------------------------------------


class TestBulkAction:
    def _setup_search(self, mock_imap_client, uids):
        def fake_search(criteria, charset="UTF-8"):
            self.captured_criteria = list(criteria)
            return list(uids)
        mock_imap_client.search.side_effect = fake_search

    def test_mark_read_by_query(self, imap_client, mock_imap_client):
        self._setup_search(mock_imap_client, [101, 102, 103])
        result = imap_client.bulk_action(
            action="mark_read", from_addr="alice@x.com", mailbox="INBOX",
        )
        assert result["matched"] == 3
        assert result["affected"] == 3
        mock_imap_client.add_flags.assert_called_once_with([101, 102, 103], [b"\\Seen"])
        assert "FROM" in self.captured_criteria

    def test_dry_run_does_not_mutate(self, imap_client, mock_imap_client):
        self._setup_search(mock_imap_client, [10, 11])
        result = imap_client.bulk_action(
            action="delete", subject="Newsletter", mailbox="INBOX", dry_run=True,
        )
        assert result["matched"] == 2
        assert result["affected"] == 0
        mock_imap_client.move.assert_not_called()
        mock_imap_client.add_flags.assert_not_called()

    def test_move_requires_destination(self, imap_client, mock_imap_client):
        self._setup_search(mock_imap_client, [1])
        with pytest.raises(ValueError, match="requires 'destination'"):
            imap_client.bulk_action(action="move", mailbox="INBOX")

    def test_move_with_destination(self, imap_client, mock_imap_client):
        self._setup_search(mock_imap_client, [1, 2])
        result = imap_client.bulk_action(
            action="move", destination="Archive", mailbox="INBOX",
        )
        assert result["affected"] == 2
        mock_imap_client.move.assert_called_once_with([1, 2], "Archive")

    def test_delete_with_permanent(self, imap_client, mock_imap_client):
        self._setup_search(mock_imap_client, [50])
        result = imap_client.bulk_action(
            action="delete", before="2026-01-01", mailbox="INBOX", permanent=True,
        )
        assert result["affected"] == 1
        mock_imap_client.add_flags.assert_called_with([50], [b"\\Deleted"])
        mock_imap_client.expunge.assert_called_once()

    def test_report_spam_bulk(self, imap_client, mock_imap_client):
        self._setup_search(mock_imap_client, [7])
        result = imap_client.bulk_action(
            action="report_spam", from_addr="spammer@x.com", mailbox="INBOX",
        )
        assert result["affected"] == 1
        assert result["moved_to"] == "Spam"

    def test_unknown_action_rejected(self, imap_client):
        with pytest.raises(ValueError, match="Unknown bulk action"):
            imap_client.bulk_action(action="vaporize")

    def test_no_matches(self, imap_client, mock_imap_client):
        self._setup_search(mock_imap_client, [])
        result = imap_client.bulk_action(action="mark_read", mailbox="INBOX")
        assert result["matched"] == 0
        assert result["affected"] == 0


# ---------------------------------------------------------------------------
# get_email_body_safe
# ---------------------------------------------------------------------------


class TestEmailBodySafe:
    def test_sanitizes_html_and_inlines_cid(self, imap_client, mock_imap_client):
        # Replace mock fetch with one returning the inline-image fixture.
        body = (
            b"MIME-Version: 1.0\r\n"
            b'Content-Type: multipart/related; boundary="b1"\r\n'
            b"\r\n"
            b"--b1\r\n"
            b"Content-Type: text/html; charset=utf-8\r\n"
            b"\r\n"
            b'<p>Hi</p><script>alert(1)</script>'
            b'<p>Logo: <img src="cid:logo123"></p>\r\n'
            b"--b1\r\n"
            b"Content-Type: image/png\r\n"
            b"Content-Disposition: inline\r\n"
            b"Content-ID: <logo123>\r\n"
            b"Content-Transfer-Encoding: base64\r\n"
            b"\r\n"
            b"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=\r\n"
            b"--b1--\r\n"
        )
        mock_imap_client.fetch.side_effect = lambda uids, fields: {
            uids[0]: {b"BODY[]": body}
        }
        result = imap_client.get_email_body_safe(uid=1, mailbox="INBOX")
        assert "<script>" not in result["html"]
        assert "alert" not in result["html"]
        assert "data:image/png;base64," in result["html"]
        assert "cid:logo123" not in result["html"]
        assert len(result["inline_images"]) == 1
        # data_uri is stripped from the response payload (it's already inlined
        # into the HTML and would bloat tool output).
        assert "data_uri" not in result["inline_images"][0]


# ---------------------------------------------------------------------------
# get_calendar_invites
# ---------------------------------------------------------------------------


class TestCalendarInvites:
    def test_returns_parsed_invite(self, imap_client, mock_imap_client):
        mock_imap_client.fetch.side_effect = lambda uids, fields: {
            uids[0]: {b"BODY[]": EMAIL_WITH_INVITE}
        }
        invites = imap_client.get_calendar_invites(uid=1, mailbox="INBOX")
        assert len(invites) == 1
        assert invites[0]["summary"] == "Quarterly review"
        assert invites[0]["organizer"] == "org@example.com"


# ---------------------------------------------------------------------------
# health_check + accounts_health server tool
# ---------------------------------------------------------------------------


class TestHealthCheck:
    def test_health_check_ok(self, imap_client, mock_imap_client):
        mock_imap_client.noop.return_value = b"OK"
        result = imap_client.health_check()
        assert result["ok"] is True
        assert result["connected"] is True
        mock_imap_client.noop.assert_called_once()

    def test_health_check_noop_fails(self, imap_client, mock_imap_client):
        mock_imap_client.noop.side_effect = Exception("connection lost")
        result = imap_client.health_check()
        assert result["ok"] is False
        assert "connection lost" in result["reason"]

    def test_health_check_disconnected(self, imap_client):
        imap_client.client = None
        result = imap_client.health_check()
        assert result["ok"] is False
        assert result["connected"] is False
