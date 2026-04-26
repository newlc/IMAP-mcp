"""Tests for write-mode tools (send_email, reply_email, forward_email, delete_email)
and for the --write CLI gating logic in server.py."""

import asyncio
import email
import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest

from imap_mcp import server as srv
from tests.conftest import MULTIPART_BODY, make_envelope, make_fetch_response


def decoded_text_body(raw_msg: bytes) -> str:
    """Parse a sent MIME message and return its decoded text/plain body."""
    msg = email.message_from_bytes(raw_msg)
    if msg.is_multipart():
        for part in msg.walk():
            disp = str(part.get("Content-Disposition", ""))
            if "attachment" in disp:
                continue
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                charset = part.get_content_charset() or "utf-8"
                return payload.decode(charset, errors="replace") if payload else ""
        return ""
    payload = msg.get_payload(decode=True)
    charset = msg.get_content_charset() or "utf-8"
    return payload.decode(charset, errors="replace") if payload else ""


# ---------------------------------------------------------------------------
# delete_email
# ---------------------------------------------------------------------------


class TestDeleteEmail:
    def test_delete_moves_to_trash_by_default(self, imap_client, mock_imap_client):
        result = imap_client.delete_email(uids=[101], mailbox="INBOX")
        assert result == {"deleted": 1, "permanent": False, "moved_to": "Trash"}
        mock_imap_client.move.assert_called_once_with([101], "Trash")
        mock_imap_client.expunge.assert_not_called()

    def test_delete_uses_custom_trash_folder(self, imap_client, mock_imap_client):
        result = imap_client.delete_email(
            uids=[101, 102], mailbox="INBOX", trash_folder="Bin"
        )
        assert result["moved_to"] == "Bin"
        mock_imap_client.move.assert_called_once_with([101, 102], "Bin")

    def test_delete_falls_back_to_config_trash(self, imap_client, mock_imap_client):
        imap_client.config["folders"]["trash"] = "Корзина"
        result = imap_client.delete_email(uids=[101])
        assert result["moved_to"] == "Корзина"
        mock_imap_client.move.assert_called_once_with([101], "Корзина")

    def test_delete_namespace_retry(self, imap_client, mock_imap_client):
        mock_imap_client.move.side_effect = [Exception("namespace error"), True]
        result = imap_client.delete_email(uids=[101], trash_folder="Trash")
        assert result["moved_to"] == "INBOX.Trash"
        assert mock_imap_client.move.call_count == 2

    def test_delete_permanent_uses_expunge(self, imap_client, mock_imap_client):
        result = imap_client.delete_email(uids=[101, 102], permanent=True)
        assert result == {"deleted": 2, "permanent": True, "moved_to": None}
        mock_imap_client.add_flags.assert_called_once_with([101, 102], [b"\\Deleted"])
        mock_imap_client.expunge.assert_called_once()
        mock_imap_client.move.assert_not_called()


# ---------------------------------------------------------------------------
# send_email
# ---------------------------------------------------------------------------


class TestSendEmail:
    def test_send_email_via_starttls(self, imap_client, mock_imap_client):
        smtp_instance = MagicMock()
        with patch("imap_mcp.imap_client.smtplib.SMTP", return_value=smtp_instance) as smtp_cls:
            result = imap_client.send_email(
                to=["dest@example.com"],
                subject="Hello",
                body="Hi there",
            )

        smtp_cls.assert_called_once_with("smtp.example.com", 587)
        smtp_instance.starttls.assert_called_once()
        smtp_instance.login.assert_called_once_with("user@example.com", "secret")
        smtp_instance.sendmail.assert_called_once()
        envelope_from, envelope_to, raw_msg = smtp_instance.sendmail.call_args[0]
        assert envelope_from == "user@example.com"
        assert envelope_to == ["dest@example.com"]
        assert b"To: dest@example.com" in raw_msg
        assert b"Subject: Hello" in raw_msg
        assert b"From: Test User <user@example.com>" in raw_msg
        assert b"Message-ID:" in raw_msg
        assert result["sent"] is True
        assert result["saved_to_sent"] == "Sent"
        # Copy appended to Sent folder (\Seen flag)
        mock_imap_client.append.assert_called_once()
        assert mock_imap_client.append.call_args[0][0] == "Sent"
        assert mock_imap_client.append.call_args[1]["flags"] == [b"\\Seen"]

    def test_send_email_smtps(self, imap_client):
        imap_client.config["smtp"] = {"host": "smtp.example.com", "port": 465, "secure": True}
        smtp_instance = MagicMock()
        with patch("imap_mcp.imap_client.smtplib.SMTP_SSL", return_value=smtp_instance) as smtp_cls, \
             patch("imap_mcp.imap_client.smtplib.SMTP") as plain_cls:
            imap_client.send_email(to=["x@example.com"], subject="s", body="b")
        smtp_cls.assert_called_once()
        plain_cls.assert_not_called()
        smtp_instance.starttls.assert_not_called()

    def test_send_email_bcc_in_envelope_not_in_headers(self, imap_client):
        smtp_instance = MagicMock()
        with patch("imap_mcp.imap_client.smtplib.SMTP", return_value=smtp_instance):
            imap_client.send_email(
                to=["a@example.com"],
                cc=["c@example.com"],
                bcc=["secret@example.com"],
                subject="s",
                body="b",
            )
        envelope_from, envelope_to, raw_msg = smtp_instance.sendmail.call_args[0]
        assert "secret@example.com" in envelope_to
        assert "c@example.com" in envelope_to
        assert b"secret@example.com" not in raw_msg
        assert b"c@example.com" in raw_msg  # Cc IS in headers

    def test_send_email_with_attachment(self, imap_client, tmp_path):
        attach_path = tmp_path / "report.pdf"
        attach_path.write_bytes(b"%PDF-1.4 fake content")

        smtp_instance = MagicMock()
        with patch("imap_mcp.imap_client.smtplib.SMTP", return_value=smtp_instance):
            imap_client.send_email(
                to=["a@example.com"],
                subject="s",
                body="b",
                attachments=[str(attach_path)],
            )
        _, _, raw_msg = smtp_instance.sendmail.call_args[0]
        assert b"multipart/mixed" in raw_msg
        assert b'filename="report.pdf"' in raw_msg
        assert b"application/pdf" in raw_msg

    def test_send_email_missing_attachment_raises(self, imap_client):
        with patch("imap_mcp.imap_client.smtplib.SMTP", return_value=MagicMock()):
            with pytest.raises(FileNotFoundError):
                imap_client.send_email(
                    to=["a@example.com"], subject="s", body="b",
                    attachments=["/nonexistent/file.pdf"],
                )

    def test_send_email_save_to_sent_disabled(self, imap_client, mock_imap_client):
        with patch("imap_mcp.imap_client.smtplib.SMTP", return_value=MagicMock()):
            result = imap_client.send_email(
                to=["a@example.com"], subject="s", body="b",
                save_to_sent=False,
            )
        assert result["saved_to_sent"] is None
        mock_imap_client.append.assert_not_called()

    def test_send_email_no_smtp_config_raises(self, imap_client):
        imap_client.config.pop("smtp", None)
        with pytest.raises(RuntimeError, match="SMTP not configured"):
            imap_client.send_email(to=["a@example.com"], subject="s", body="b")

    def test_send_email_no_credentials_raises(self, imap_client):
        imap_client.config["credentials"]["password"] = ""
        with patch("imap_mcp.imap_client.get_stored_password", return_value=None):
            with pytest.raises(RuntimeError, match="No SMTP credentials"):
                imap_client.send_email(to=["a@example.com"], subject="s", body="b")

    def test_send_email_resolves_password_from_keyring(self, imap_client):
        imap_client.config["credentials"]["password"] = ""
        smtp_instance = MagicMock()
        with patch("imap_mcp.imap_client.smtplib.SMTP", return_value=smtp_instance), \
             patch("imap_mcp.imap_client.get_stored_password", return_value="kr-password"):
            imap_client.send_email(to=["a@example.com"], subject="s", body="b",
                                   save_to_sent=False)
        smtp_instance.login.assert_called_once_with("user@example.com", "kr-password")


# ---------------------------------------------------------------------------
# reply_email
# ---------------------------------------------------------------------------


REPLY_BODY = (
    b"From: Alice <alice@example.com>\r\n"
    b"To: user@example.com, bob@example.com\r\n"
    b"Cc: carol@example.com\r\n"
    b"Subject: Original Subject\r\n"
    b"Date: Mon, 20 Apr 2026 10:30:00 +0000\r\n"
    b"Message-ID: <orig-001@example.com>\r\n"
    b"References: <prev-001@example.com>\r\n"
    b"Content-Type: text/plain; charset=utf-8\r\n"
    b"\r\n"
    b"Original body line 1\r\n"
    b"Original body line 2\r\n"
)


class TestReplyEmail:
    def _setup_fetch(self, mock_imap_client, body=REPLY_BODY):
        mock_imap_client.fetch.side_effect = lambda uids, fields: {
            uids[0]: {b"BODY[]": body, b"ENVELOPE": make_envelope(), b"FLAGS": (), b"RFC822.SIZE": len(body)}
        }

    def test_reply_to_sender_only(self, imap_client, mock_imap_client):
        self._setup_fetch(mock_imap_client)
        smtp_instance = MagicMock()
        with patch("imap_mcp.imap_client.smtplib.SMTP", return_value=smtp_instance):
            imap_client.reply_email(uid=101, body="Got it.", mailbox="INBOX")
        envelope_from, envelope_to, raw_msg = smtp_instance.sendmail.call_args[0]
        assert envelope_to == ["alice@example.com"]
        assert b"Subject: Re: Original Subject" in raw_msg
        assert b"In-Reply-To: <orig-001@example.com>" in raw_msg
        assert b"References: <prev-001@example.com> <orig-001@example.com>" in raw_msg
        body_text = decoded_text_body(raw_msg)
        assert "On Mon, 20 Apr 2026 10:30:00 +0000" in body_text
        assert "> Original body line 1" in body_text

    def test_reply_all_excludes_self(self, imap_client, mock_imap_client):
        self._setup_fetch(mock_imap_client)
        smtp_instance = MagicMock()
        with patch("imap_mcp.imap_client.smtplib.SMTP", return_value=smtp_instance):
            imap_client.reply_email(
                uid=101, body="To everyone", mailbox="INBOX", reply_all=True
            )
        _, envelope_to, raw_msg = smtp_instance.sendmail.call_args[0]
        # alice in To, bob+carol in Cc, user@example.com (self) excluded
        assert "alice@example.com" in envelope_to
        assert "bob@example.com" in envelope_to
        assert "carol@example.com" in envelope_to
        assert "user@example.com" not in envelope_to
        assert b"Cc: bob@example.com, carol@example.com" in raw_msg

    def test_reply_skips_redundant_re_prefix(self, imap_client, mock_imap_client):
        body = REPLY_BODY.replace(b"Subject: Original Subject",
                                  b"Subject: Re: Already replied")
        self._setup_fetch(mock_imap_client, body=body)
        smtp_instance = MagicMock()
        with patch("imap_mcp.imap_client.smtplib.SMTP", return_value=smtp_instance):
            imap_client.reply_email(uid=101, body="x", mailbox="INBOX")
        _, _, raw_msg = smtp_instance.sendmail.call_args[0]
        assert b"Subject: Re: Already replied" in raw_msg
        assert b"Subject: Re: Re:" not in raw_msg

    def test_reply_no_quote(self, imap_client, mock_imap_client):
        self._setup_fetch(mock_imap_client)
        smtp_instance = MagicMock()
        with patch("imap_mcp.imap_client.smtplib.SMTP", return_value=smtp_instance):
            imap_client.reply_email(
                uid=101, body="Just my reply.", quote_original=False, mailbox="INBOX"
            )
        _, _, raw_msg = smtp_instance.sendmail.call_args[0]
        body_text = decoded_text_body(raw_msg)
        assert "> Original body line" not in body_text
        assert "Just my reply." in body_text

    def test_reply_uses_reply_to_header(self, imap_client, mock_imap_client):
        body = REPLY_BODY.replace(
            b"From: Alice <alice@example.com>\r\n",
            b"From: Alice <alice@example.com>\r\nReply-To: replies@example.com\r\n",
        )
        self._setup_fetch(mock_imap_client, body=body)
        smtp_instance = MagicMock()
        with patch("imap_mcp.imap_client.smtplib.SMTP", return_value=smtp_instance):
            imap_client.reply_email(uid=101, body="x", mailbox="INBOX")
        _, envelope_to, _ = smtp_instance.sendmail.call_args[0]
        assert envelope_to == ["replies@example.com"]


# ---------------------------------------------------------------------------
# forward_email
# ---------------------------------------------------------------------------


class TestForwardEmail:
    def _setup_fetch(self, mock_imap_client, body=MULTIPART_BODY):
        mock_imap_client.fetch.side_effect = lambda uids, fields: {
            uids[0]: {b"BODY[]": body, b"ENVELOPE": make_envelope(), b"FLAGS": (), b"RFC822.SIZE": len(body)}
        }

    def test_forward_preserves_attachments(self, imap_client, mock_imap_client):
        self._setup_fetch(mock_imap_client)
        smtp_instance = MagicMock()
        with patch("imap_mcp.imap_client.smtplib.SMTP", return_value=smtp_instance):
            imap_client.forward_email(
                uid=101, to=["fwd@example.com"], body="FYI", mailbox="INBOX"
            )
        _, envelope_to, raw_msg = smtp_instance.sendmail.call_args[0]
        assert envelope_to == ["fwd@example.com"]
        assert b"Subject: Fwd: Test with attachment" in raw_msg
        body_text = decoded_text_body(raw_msg)
        assert "---------- Forwarded message ----------" in body_text
        assert b'filename="report.pdf"' in raw_msg

    def test_forward_skip_attachments(self, imap_client, mock_imap_client):
        self._setup_fetch(mock_imap_client)
        smtp_instance = MagicMock()
        with patch("imap_mcp.imap_client.smtplib.SMTP", return_value=smtp_instance):
            imap_client.forward_email(
                uid=101, to=["fwd@example.com"], body="",
                include_attachments=False, mailbox="INBOX",
            )
        _, _, raw_msg = smtp_instance.sendmail.call_args[0]
        assert b'filename="report.pdf"' not in raw_msg

    def test_forward_no_double_fwd_prefix(self, imap_client, mock_imap_client):
        body = MULTIPART_BODY.replace(b"Subject: Test with attachment",
                                      b"Subject: Fwd: Already forwarded")
        self._setup_fetch(mock_imap_client, body=body)
        smtp_instance = MagicMock()
        with patch("imap_mcp.imap_client.smtplib.SMTP", return_value=smtp_instance):
            imap_client.forward_email(uid=101, to=["x@example.com"], mailbox="INBOX")
        _, _, raw_msg = smtp_instance.sendmail.call_args[0]
        assert b"Subject: Fwd: Already forwarded" in raw_msg
        assert b"Subject: Fwd: Fwd:" not in raw_msg


# ---------------------------------------------------------------------------
# Server-level --write gating
# ---------------------------------------------------------------------------


class TestWriteModeGating:
    def setup_method(self, method):
        # Reset module state so tests don't bleed into one another.
        srv._write_enabled = False
        srv.account_manager.accounts.clear()
        srv.account_manager.default_name = None

    def teardown_method(self, method):
        srv._write_enabled = False
        srv.account_manager.accounts.clear()
        srv.account_manager.default_name = None

    def test_list_tools_excludes_write_tools_by_default(self):
        srv._write_enabled = False
        tools = asyncio.run(srv.list_tools())
        names = {t.name for t in tools}
        assert "send_email" not in names
        assert "reply_email" not in names
        assert "forward_email" not in names
        assert "delete_email" not in names
        assert "rename_mailbox" not in names
        assert "delete_mailbox" not in names
        assert "empty_mailbox" not in names
        assert "sieve_put_script" not in names
        # Read-only tools still present
        assert "fetch_emails" in names
        assert "save_draft" in names
        assert "move_email" in names
        assert "archive_email" in names
        assert "search_emails_fts" in names
        assert "report_spam" in names
        assert "get_capabilities" in names
        assert "list_accounts" in names

    def test_list_tools_includes_write_tools_when_enabled(self):
        srv._write_enabled = True
        tools = asyncio.run(srv.list_tools())
        names = {t.name for t in tools}
        assert {
            "send_email", "reply_email", "forward_email", "delete_email",
            "rename_mailbox", "delete_mailbox", "empty_mailbox",
            "sieve_put_script", "sieve_delete_script", "sieve_activate_script",
        } <= names

    def test_every_tool_advertises_account_param(self):
        srv._write_enabled = True
        tools = asyncio.run(srv.list_tools())
        global_tools = {"auto_connect", "list_accounts", "disconnect", "accounts_health"}
        for t in tools:
            if t.name in global_tools:
                continue
            props = t.inputSchema.get("properties", {})
            assert "account" in props, (
                f"Tool {t.name!r} does not advertise an 'account' parameter"
            )

    def test_write_tool_call_blocked_when_disabled(self):
        srv._write_enabled = False
        with pytest.raises(PermissionError, match="read-only"):
            asyncio.run(srv.handle_tool_call("send_email", {
                "to": ["x@example.com"], "subject": "s", "body": "b",
            }))

    def test_write_tool_call_returns_error_text_via_call_tool(self):
        srv._write_enabled = False
        result = asyncio.run(srv.call_tool("delete_email", {"uids": [1]}))
        assert len(result) == 1
        assert "read-only" in result[0].text

    def test_write_tool_call_proceeds_when_enabled(self, single_account_manager):
        srv._write_enabled = True
        srv.account_manager.accounts.update(single_account_manager.accounts)
        srv.account_manager.default_name = single_account_manager.default_name

        called = {}

        def fake_send_email(**kwargs):
            called.update(kwargs)
            return {"sent": True, "message_id": "<x@y>", "saved_to_sent": "Sent"}

        single_account_manager.get(None).send_email = fake_send_email
        result = asyncio.run(srv.handle_tool_call("send_email", {
            "to": ["x@example.com"], "subject": "s", "body": "b",
        }))
        assert result["sent"] is True
        assert called["to"] == ["x@example.com"]


# ---------------------------------------------------------------------------
# save_draft attachments
# ---------------------------------------------------------------------------


class TestSaveDraftAttachments:
    def test_save_draft_with_attachment(self, imap_client, mock_imap_client, tmp_path):
        attach_path = tmp_path / "spec.txt"
        attach_path.write_bytes(b"contents of spec file")
        result = imap_client.save_draft(
            to=["test@example.com"],
            subject="Draft with attachment",
            body="See attached.",
            attachments=[str(attach_path)],
        )
        assert result["saved"] is True
        msg_bytes = mock_imap_client.append.call_args[0][1]
        assert b"multipart/mixed" in msg_bytes
        assert b'filename="spec.txt"' in msg_bytes
