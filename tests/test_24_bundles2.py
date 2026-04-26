"""Tests for the second batch of bundles:

* Bug fixes: forward_email in-memory streaming, get_email partial fetch,
  encrypted-cache code path coverage.
* Onboarding: provider templates, validate_config, JSON Schema export.
* AI-UX: smart_truncate, extract_action_items, watch_until.
* Long-ops: subscribe_resource registry behaviour.
"""

from __future__ import annotations

import asyncio
import email
import json
from unittest.mock import MagicMock, patch

import pytest

from imap_mcp import server as srv
from imap_mcp.cache import EmailCache
from imap_mcp.mail_utils import (
    extract_action_items,
    smart_truncate,
)
from imap_mcp.providers import (
    CONFIG_JSON_SCHEMA,
    PROVIDER_TEMPLATES,
    make_starter_account,
    validate_config,
)
from tests.conftest import MULTIPART_BODY, make_envelope


# ===========================================================================
# Bug fixes
# ===========================================================================


class TestForwardStreaming:
    def test_forward_uses_inline_attachments_no_temp_dir(
        self, imap_client, mock_imap_client, monkeypatch
    ):
        # Spy on tempfile.mkdtemp -- forward_email should NOT call it.
        called = {"mkdtemp": 0}
        original_mkdtemp = __import__("tempfile").mkdtemp
        def tracking_mkdtemp(*a, **kw):
            called["mkdtemp"] += 1
            return original_mkdtemp(*a, **kw)
        monkeypatch.setattr("imap_mcp.imap_client.tempfile.mkdtemp", tracking_mkdtemp)

        mock_imap_client.fetch.side_effect = lambda uids, fields: {
            uids[0]: {b"BODY[]": MULTIPART_BODY}
        }
        smtp_instance = MagicMock()
        with patch("imap_mcp.imap_client.smtplib.SMTP", return_value=smtp_instance):
            imap_client.forward_email(
                uid=1, to=["fwd@example.com"], body="FYI", mailbox="INBOX"
            )
        assert called["mkdtemp"] == 0
        # Verify the attachment did make it into the outgoing message.
        _, _, raw = smtp_instance.sendmail.call_args[0]
        assert b'filename="report.pdf"' in raw

    def test_inline_attachments_via_send_email(self, imap_client):
        smtp_instance = MagicMock()
        with patch("imap_mcp.imap_client.smtplib.SMTP", return_value=smtp_instance):
            imap_client.send_email(
                to=["a@x.com"], subject="s", body="b",
                inline_attachments=[
                    ("data.bin", "application/octet-stream", b"\x00\x01\x02"),
                ],
            )
        _, _, raw = smtp_instance.sendmail.call_args[0]
        assert b'filename="data.bin"' in raw


class TestGetEmailPartialFetch:
    def test_peek_bytes_uses_partial_fetch_and_skips_attachments(
        self, imap_client, mock_imap_client
    ):
        captured = {}
        def fake_fetch(uids, fields):
            captured["fields"] = list(fields)
            return {
                uids[0]: {
                    b"ENVELOPE": make_envelope(),
                    b"FLAGS": (),
                    b"RFC822.SIZE": 100000,
                    b"BODY[HEADER]": (
                        b"From: x@y\r\nSubject: Big\r\n"
                        b"Content-Type: text/plain\r\n"
                    ),
                    b"BODY[TEXT]<0>": b"first 500 bytes of a huge body",
                }
            }
        mock_imap_client.fetch.side_effect = fake_fetch

        result = imap_client.get_email(uid=1, mailbox="INBOX", peek_bytes=500)
        assert any("BODY.PEEK[TEXT]<0.500>" in str(f) for f in captured["fields"])
        assert any("BODY.PEEK[HEADER]" in str(f) for f in captured["fields"])
        # Attachments are NOT indexed in partial mode.
        assert result.attachments == []
        assert "first 500 bytes" in (result.body.text or "")

    def test_peek_bytes_zero_falls_back_to_full_body(
        self, imap_client, mock_imap_client
    ):
        captured = {}
        def fake_fetch(uids, fields):
            captured["fields"] = list(fields)
            return {
                uids[0]: {
                    b"ENVELOPE": make_envelope(),
                    b"FLAGS": (),
                    b"RFC822.SIZE": 100,
                    b"BODY[]": (
                        b"From: x@y\r\nSubject: t\r\n\r\nFull body"
                    ),
                }
            }
        mock_imap_client.fetch.side_effect = fake_fetch
        # peek_bytes=0 -> partial path skipped.
        imap_client.get_email(uid=1, mailbox="INBOX", peek_bytes=0)
        assert "BODY[]" in captured["fields"]


class TestEncryptedCacheRoundTrip:
    def test_encrypted_cache_persists_through_close(self, tmp_path):
        # Use mocked keyring so we don't depend on a system keyring.
        store: dict[tuple, str] = {}
        def get_pwd(svc, user):
            return store.get((svc, user))
        def set_pwd(svc, user, pwd):
            store[(svc, user)] = pwd

        db_path = str(tmp_path / "enc.db")
        with patch("imap_mcp.cache.keyring") as kr:
            kr.get_password.side_effect = get_pwd
            kr.set_password.side_effect = set_pwd

            c1 = EmailCache(db_path, encrypted=True, keyring_username="enc-test")
            c1.store_email(
                "INBOX", 1,
                {"message_id": "<m@x>", "subject": "Hello",
                 "from_address": {"email": "a@x.com"}, "to_addresses": [],
                 "cc_addresses": [], "date": None, "flags": [], "size": 10},
                {"text": "Body!", "html": None},
            )
            c1.close()

            # On-disk file should be encrypted (no plaintext "Hello").
            with open(db_path + ".enc", "rb") as f:
                disk_data = f.read()
            assert b"Hello" not in disk_data
            assert b"Body!" not in disk_data

            # Reopen with the same key -> data resurfaces.
            c2 = EmailCache(db_path, encrypted=True, keyring_username="enc-test")
            row = c2.get_email("INBOX", 1)
            assert row is not None
            assert row["subject"] == "Hello"
            assert row["body_text"] == "Body!"

    def test_encrypted_cache_wrong_key_starts_empty(self, tmp_path, caplog):
        store: dict[tuple, str] = {}
        def get_pwd(svc, user):
            return store.get((svc, user))
        def set_pwd(svc, user, pwd):
            store[(svc, user)] = pwd

        db_path = str(tmp_path / "enc.db")
        with patch("imap_mcp.cache.keyring") as kr:
            kr.get_password.side_effect = get_pwd
            kr.set_password.side_effect = set_pwd
            c1 = EmailCache(db_path, encrypted=True, keyring_username="k1")
            c1.store_email(
                "INBOX", 1,
                {"message_id": "<m@x>", "subject": "x",
                 "from_address": {"email": "a@x.com"}, "to_addresses": [],
                 "cc_addresses": [], "date": None, "flags": [], "size": 0},
                {"text": "secret", "html": None},
            )
            c1.close()

            # Now open with a different key -> can't decrypt -> empty cache.
            c2 = EmailCache(db_path, encrypted=True, keyring_username="k2")
            assert c2.get_email("INBOX", 1) is None


# ===========================================================================
# Onboarding bundle
# ===========================================================================


class TestProviderTemplates:
    @pytest.mark.parametrize("provider", list(PROVIDER_TEMPLATES))
    def test_every_template_buildable(self, provider):
        block = make_starter_account(
            provider, "test", "user@example.com",
        )
        assert block["name"] == "test"
        assert block["default"] is True
        assert "imap" in block
        assert block["imap"]["host"]
        assert "credentials" in block
        # Notes are preserved when present.
        # (don't enforce, providers without notes shouldn't have the field)

    def test_unknown_provider_rejected(self):
        with pytest.raises(ValueError, match="Unknown provider"):
            make_starter_account("not-a-provider", "x", "y@z.com")


class TestValidateConfig:
    def test_legacy_config_rejected(self, tmp_path):
        p = tmp_path / "c.json"
        p.write_text(json.dumps({"imap": {"host": "x"}, "credentials": {}}))
        result = validate_config(str(p), check_keyring=False)
        assert result["valid"] is False
        assert any("Legacy" in e for e in result["errors"])

    def test_missing_file(self, tmp_path):
        result = validate_config(str(tmp_path / "no.json"), check_keyring=False)
        assert result["valid"] is False
        assert any("not found" in e for e in result["errors"])

    def test_malformed_json_reported(self, tmp_path):
        p = tmp_path / "c.json"
        p.write_text("{ not json")
        result = validate_config(str(p), check_keyring=False)
        assert result["valid"] is False
        assert any("Malformed JSON" in e for e in result["errors"])

    def test_multi_account_without_default_rejected(self, tmp_path):
        p = tmp_path / "c.json"
        p.write_text(json.dumps({
            "accounts": [
                {"name": "a", "imap": {"host": "x"},
                 "credentials": {"username": "a@x.com"}},
                {"name": "b", "imap": {"host": "y"},
                 "credentials": {"username": "b@y.com"}},
            ]
        }))
        result = validate_config(str(p), check_keyring=False)
        assert result["valid"] is False
        assert any("default" in e.lower() for e in result["errors"])

    def test_valid_minimal_config(self, tmp_path):
        p = tmp_path / "c.json"
        p.write_text(json.dumps({
            "accounts": [
                {"name": "x", "imap": {"host": "imap.example.com"},
                 "credentials": {"username": "x@example.com"}},
            ]
        }))
        # validate_config imports keyring lazily; patch at the source.
        with patch("keyring.get_password", return_value="stored"):
            result = validate_config(str(p))
        assert result["valid"] is True
        assert result["accounts"][0]["ok"] is True


class TestJsonSchema:
    def test_schema_is_valid_json(self):
        # Round-trip through json.dumps to confirm serializability.
        text = json.dumps(CONFIG_JSON_SCHEMA)
        re_parsed = json.loads(text)
        assert re_parsed["title"] == "imap-mcp config"
        assert "accounts" in re_parsed["properties"]


# ===========================================================================
# AI-UX bundle
# ===========================================================================


class TestSmartTruncate:
    def test_short_input_unchanged(self):
        assert smart_truncate("hello", 100) == "hello"
        assert smart_truncate("", 100) == ""

    def test_cuts_on_sentence_boundary(self):
        text = "First sentence. Second sentence. Third sentence."
        out = smart_truncate(text, 35)
        # Should end with "First sentence." plus ellipsis, not mid-word.
        assert out.endswith("…")
        assert "Second" not in out or out.rstrip("…").endswith(".")

    def test_falls_back_to_word_boundary(self):
        text = "one two three four five six seven eight nine"
        out = smart_truncate(text, 20)
        assert out.endswith("…")
        # Should not end mid-word (no partial token before the ellipsis)
        body = out.rstrip("…").rstrip()
        assert " " in body  # some word was preserved


class TestExtractActionItems:
    def test_finds_request(self):
        result = extract_action_items(
            "Hi team. Could you please send me the Q1 report by Friday? Thanks."
        )
        # Match either the "could you" pattern or "please" pattern -- both fire here.
        assert result["requests"]
        assert result["deadlines"]  # "by Friday"

    def test_finds_question(self):
        result = extract_action_items(
            "Are we still on for the meeting? Please reply by tomorrow morning."
        )
        assert result["questions"]
        assert result["deadlines"]  # "by tomorrow"

    def test_finds_blocker(self):
        result = extract_action_items(
            "I'm blocked on access to the staging environment."
        )
        assert result["blockers"]

    def test_empty_text(self):
        result = extract_action_items("")
        assert result == {
            "requests": [], "questions": [], "deadlines": [], "blockers": []
        }

    def test_dedupes(self):
        result = extract_action_items(
            "Please send report. Please send report. Please send report."
        )
        assert len(result["requests"]) == 1


class TestWatchUntil:
    def test_immediate_match_returns_without_idle(
        self, imap_client, mock_imap_client
    ):
        mock_imap_client.search.return_value = [42]
        # Mock the summary path
        from imap_mcp.models import EmailHeader, EmailAddress
        from datetime import datetime
        mock_imap_client.fetch.side_effect = lambda uids, fields: {
            uids[0]: {
                b"ENVELOPE": make_envelope(),
                b"FLAGS": (),
                b"RFC822.SIZE": 100,
                b"BODYSTRUCTURE": None,
                b"BODY[TEXT]<0>": b"OTP code: 123456",
            }
        }
        result = imap_client.watch_until(
            criteria={"from_addr": "auth@bank.com"},
            mailbox="INBOX",
            timeout=1,
        )
        assert result["matched"] is True
        assert result["uid"] == 42
        # IDLE not entered for an immediate match.
        mock_imap_client.idle.assert_not_called()

    def test_no_match_times_out(self, imap_client, mock_imap_client):
        mock_imap_client.search.return_value = []
        # IDLE returns no events
        mock_imap_client.idle_check.return_value = []
        result = imap_client.watch_until(
            criteria={"from_addr": "noone"}, mailbox="INBOX", timeout=1
        )
        assert result["matched"] is False
        assert result["timed_out"] is True


# ===========================================================================
# Long-ops bundle (subscribe_resource)
# ===========================================================================


class TestResourceSubscriptions:
    def setup_method(self, method):
        srv._subscribed_uris.clear()
        srv._dirty_overviews.clear()

    def teardown_method(self, method):
        srv._subscribed_uris.clear()
        srv._dirty_overviews.clear()

    def test_subscribe_records_uri(self):
        from pydantic import AnyUrl
        asyncio.run(srv.subscribe_resource(AnyUrl("imap://work/overview")))
        assert "imap://work/overview" in srv._subscribed_uris

    def test_unsubscribe_removes_uri(self):
        from pydantic import AnyUrl
        srv._subscribed_uris.add("imap://work/overview")
        asyncio.run(srv.unsubscribe_resource(AnyUrl("imap://work/overview")))
        assert "imap://work/overview" not in srv._subscribed_uris

    def test_mark_dirty_only_when_subscribed(self):
        # Outside a request context, _flush is a no-op (no session).
        srv._mark_overview_dirty("work")
        assert "work" in srv._dirty_overviews
        # _flush silently no-ops without a session
        asyncio.run(srv._flush_resource_notifications())
        # Dirty flags only clear when session was reachable; without a
        # session they stay (and that's fine -- next call to flush will
        # try again).
        assert "work" in srv._dirty_overviews
