"""Tests for mail_utils: HTML sanitization, inline images, calendar parsing,
and the retry helper."""

import email
import time
from unittest.mock import MagicMock, patch

import pytest

from imap_mcp.mail_utils import (
    extract_inline_images,
    inline_cid_to_data_uri,
    parse_calendar_invites,
    sanitize_html,
    with_retries,
)


# ---------------------------------------------------------------------------
# HTML sanitization
# ---------------------------------------------------------------------------


class TestSanitizeHtml:
    def test_strips_script(self):
        out = sanitize_html("<p>Hello</p><script>alert('x')</script>")
        assert "<script>" not in out
        assert "alert" not in out
        assert "Hello" in out

    def test_strips_style(self):
        out = sanitize_html("<style>body{font:huge}</style><p>Hi</p>")
        assert "<style>" not in out
        assert "huge" not in out

    def test_strips_event_handlers(self):
        out = sanitize_html('<a href="https://x" onclick="evil()">click</a>')
        assert "onclick" not in out
        assert "evil" not in out

    def test_strips_javascript_urls(self):
        out = sanitize_html('<a href="javascript:alert(1)">click</a>')
        assert "javascript:" not in out

    def test_keeps_safe_tags(self):
        html = "<p><strong>Bold</strong> and <em>italic</em></p>"
        out = sanitize_html(html)
        assert "<strong>" in out
        assert "<em>" in out

    def test_strips_iframe(self):
        out = sanitize_html('<iframe src="https://evil.com"></iframe>')
        assert "<iframe" not in out

    def test_keeps_safe_style_props_drops_others(self):
        out = sanitize_html(
            '<p style="color:red;position:absolute;background:yellow">x</p>'
        )
        assert "color:" in out or "color :" in out  # bleach normalizes whitespace
        assert "position" not in out

    def test_strip_remote_images(self):
        html = '<img src="https://tracker.com/pixel.gif"><img src="cid:logo">'
        out = sanitize_html(html, strip_remote_images=True)
        assert "tracker.com" not in out
        assert "cid:logo" in out

    def test_strip_links(self):
        html = '<a href="https://x.com">click</a>'
        out = sanitize_html(html, strip_links=True)
        assert "x.com" not in out
        assert 'href="#"' in out

    def test_empty_input(self):
        assert sanitize_html("") == ""
        assert sanitize_html(None) == ""


# ---------------------------------------------------------------------------
# Inline images
# ---------------------------------------------------------------------------

EMAIL_WITH_INLINE_IMAGE = (
    b"MIME-Version: 1.0\r\n"
    b"From: sender@example.com\r\n"
    b"Subject: Inline image test\r\n"
    b'Content-Type: multipart/related; boundary="b1"\r\n'
    b"\r\n"
    b"--b1\r\n"
    b"Content-Type: text/html; charset=utf-8\r\n"
    b"\r\n"
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


class TestInlineImages:
    def test_extract_inline_images(self):
        msg = email.message_from_bytes(EMAIL_WITH_INLINE_IMAGE)
        imgs = extract_inline_images(msg)
        assert len(imgs) == 1
        assert imgs[0]["cid"] == "logo123"
        assert imgs[0]["content_type"] == "image/png"
        assert imgs[0]["data_uri"].startswith("data:image/png;base64,")

    def test_inline_cid_replacement(self):
        msg = email.message_from_bytes(EMAIL_WITH_INLINE_IMAGE)
        imgs = extract_inline_images(msg)
        html = '<p>Logo: <img src="cid:logo123"></p>'
        out = inline_cid_to_data_uri(html, imgs)
        assert "cid:logo123" not in out
        assert "data:image/png;base64," in out

    def test_inline_no_match_unchanged(self):
        html = '<img src="cid:nothere">'
        imgs = [{"cid": "elsewhere", "data_uri": "data:..."}]
        out = inline_cid_to_data_uri(html, imgs)
        assert out == html


# ---------------------------------------------------------------------------
# Calendar invites
# ---------------------------------------------------------------------------

EMAIL_WITH_INVITE = (
    b"MIME-Version: 1.0\r\n"
    b"From: org@example.com\r\n"
    b"To: invitee@example.com\r\n"
    b"Subject: Quarterly review\r\n"
    b'Content-Type: multipart/mixed; boundary="b1"\r\n'
    b"\r\n"
    b"--b1\r\n"
    b"Content-Type: text/plain\r\n"
    b"\r\n"
    b"Please come.\r\n"
    b"--b1\r\n"
    b'Content-Type: text/calendar; method=REQUEST\r\n'
    b"\r\n"
    b"BEGIN:VCALENDAR\r\n"
    b"VERSION:2.0\r\n"
    b"METHOD:REQUEST\r\n"
    b"BEGIN:VEVENT\r\n"
    b"UID:event-001@example.com\r\n"
    b"SUMMARY:Quarterly review\r\n"
    b"DESCRIPTION:Annual quarterly review meeting\r\n"
    b"LOCATION:Conference Room A\r\n"
    b"DTSTART:20260615T140000Z\r\n"
    b"DTEND:20260615T150000Z\r\n"
    b"ORGANIZER:mailto:org@example.com\r\n"
    b"ATTENDEE;CN=Invitee;PARTSTAT=NEEDS-ACTION;ROLE=REQ-PARTICIPANT:mailto:invitee@example.com\r\n"
    b"SEQUENCE:0\r\n"
    b"STATUS:CONFIRMED\r\n"
    b"END:VEVENT\r\n"
    b"END:VCALENDAR\r\n"
    b"--b1--\r\n"
)


class TestCalendarInvites:
    def test_parse_invite(self):
        msg = email.message_from_bytes(EMAIL_WITH_INVITE)
        invites = parse_calendar_invites(msg)
        assert len(invites) == 1
        inv = invites[0]
        assert inv["method"] == "REQUEST"
        assert inv["uid"] == "event-001@example.com"
        assert inv["summary"] == "Quarterly review"
        assert inv["location"] == "Conference Room A"
        assert "2026-06-15" in inv["start"]
        assert "2026-06-15" in inv["end"]
        assert inv["organizer"] == "org@example.com"
        assert inv["attendees"][0]["email"] == "invitee@example.com"
        assert inv["attendees"][0]["partstat"] == "NEEDS-ACTION"
        assert inv["status"] == "CONFIRMED"

    def test_no_calendar_returns_empty(self):
        msg = email.message_from_bytes(
            b"From: x@y.com\r\nSubject: Hi\r\n\r\nBody"
        )
        assert parse_calendar_invites(msg) == []


# ---------------------------------------------------------------------------
# Retry
# ---------------------------------------------------------------------------


class TestWithRetries:
    def test_succeeds_first_try(self):
        calls = []
        def fn():
            calls.append(1)
            return "ok"
        assert with_retries(fn) == "ok"
        assert len(calls) == 1

    def test_retries_on_transient_error(self):
        calls = []
        def fn():
            calls.append(1)
            if len(calls) < 3:
                raise TimeoutError("temporary")
            return "ok"
        with patch("imap_mcp.mail_utils.time.sleep"):
            result = with_retries(fn, attempts=3, base_delay=0.01)
        assert result == "ok"
        assert len(calls) == 3

    def test_does_not_retry_non_transient(self):
        calls = []
        def fn():
            calls.append(1)
            raise ValueError("bad input")
        with pytest.raises(ValueError):
            with_retries(fn, attempts=3)
        assert len(calls) == 1

    def test_gives_up_after_attempts(self):
        def fn():
            raise ConnectionError("flap")
        with patch("imap_mcp.mail_utils.time.sleep"):
            with pytest.raises(ConnectionError):
                with_retries(fn, attempts=2, base_delay=0.01)

    def test_classifies_message_keywords_as_transient(self):
        calls = []
        def fn():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("Service unavailable, try again later")
            return "ok"
        with patch("imap_mcp.mail_utils.time.sleep"):
            assert with_retries(fn, attempts=2, base_delay=0.01) == "ok"
        assert len(calls) == 2
