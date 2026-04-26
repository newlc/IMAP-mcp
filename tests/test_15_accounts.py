"""Tests for the multi-account configuration layer (AccountManager)."""

import json

import pytest

from imap_mcp.accounts import AccountManager, migrate_legacy_config


def _write(tmp_path, raw):
    p = tmp_path / "config.json"
    p.write_text(json.dumps(raw))
    return str(p)


class TestLegacyDetection:
    def test_legacy_detected_by_top_level_imap(self):
        assert AccountManager.is_legacy_config({"imap": {}, "credentials": {}})

    def test_new_format_not_legacy(self):
        assert not AccountManager.is_legacy_config({"accounts": []})

    def test_load_legacy_raises(self, tmp_path):
        p = _write(tmp_path, {"imap": {"host": "x"}, "credentials": {"username": "u"}})
        mgr = AccountManager()
        with pytest.raises(ValueError, match="legacy single-account format"):
            mgr.load_config(p)


class TestSingleAccountLoad:
    def test_single_account_no_default_flag_is_default(self, tmp_path, monkeypatch):
        from imap_mcp import imap_client as ic

        monkeypatch.setattr(ic.ImapClientWrapper, "_connect_with_loaded_config",
                            lambda self: setattr(self, "client", object()) or True)

        p = _write(tmp_path, {"accounts": [
            {"name": "only", "imap": {"host": "h"}, "credentials": {"username": "u"},
             "cache": {"enabled": False}},
        ]})
        mgr = AccountManager()
        mgr.load_config(p)
        assert mgr.default_name == "only"
        assert list(mgr.accounts) == ["only"]

    def test_per_account_cache_defaults_applied(self, tmp_path, monkeypatch):
        p = _write(tmp_path, {"accounts": [
            {"name": "alpha", "imap": {"host": "h"}, "credentials": {"username": "u"},
             "cache": {"enabled": False}},
        ]})
        mgr = AccountManager()
        mgr.load_config(p)
        cache_cfg = mgr.accounts["alpha"].config["cache"]
        assert cache_cfg["db_path"].endswith("alpha.db")
        assert cache_cfg["keyring_username"] == "encryption-key-alpha"
        assert cache_cfg["encrypt"] is False


class TestMultiAccount:
    def test_multi_account_requires_default_flag(self, tmp_path):
        p = _write(tmp_path, {"accounts": [
            {"name": "a", "imap": {}, "credentials": {}, "cache": {"enabled": False}},
            {"name": "b", "imap": {}, "credentials": {}, "cache": {"enabled": False}},
        ]})
        mgr = AccountManager()
        with pytest.raises(ValueError, match="default: true"):
            mgr.load_config(p)

    def test_more_than_one_default_rejected(self, tmp_path):
        p = _write(tmp_path, {"accounts": [
            {"name": "a", "default": True, "imap": {}, "credentials": {},
             "cache": {"enabled": False}},
            {"name": "b", "default": True, "imap": {}, "credentials": {},
             "cache": {"enabled": False}},
        ]})
        mgr = AccountManager()
        with pytest.raises(ValueError, match="More than one"):
            mgr.load_config(p)

    def test_default_flag_picks_account(self, tmp_path):
        p = _write(tmp_path, {"accounts": [
            {"name": "a", "imap": {}, "credentials": {}, "cache": {"enabled": False}},
            {"name": "b", "default": True, "imap": {}, "credentials": {},
             "cache": {"enabled": False}},
        ]})
        mgr = AccountManager()
        mgr.load_config(p)
        assert mgr.default_name == "b"

    def test_each_account_has_own_keyring_username(self, tmp_path):
        p = _write(tmp_path, {"accounts": [
            {"name": "work", "default": True, "imap": {}, "credentials": {},
             "cache": {"enabled": False, "encrypt": True}},
            {"name": "personal", "imap": {}, "credentials": {},
             "cache": {"enabled": False, "encrypt": False}},
        ]})
        mgr = AccountManager()
        mgr.load_config(p)
        a, b = mgr.accounts["work"].config, mgr.accounts["personal"].config
        assert a["cache"]["keyring_username"] != b["cache"]["keyring_username"]
        assert a["cache"]["db_path"] != b["cache"]["db_path"]
        # User-chosen encryption settings preserved per account.
        assert a["cache"]["encrypt"] is True
        assert b["cache"]["encrypt"] is False

    def test_duplicate_names_rejected(self, tmp_path):
        p = _write(tmp_path, {"accounts": [
            {"name": "x", "default": True, "imap": {}, "credentials": {},
             "cache": {"enabled": False}},
            {"name": "x", "imap": {}, "credentials": {},
             "cache": {"enabled": False}},
        ]})
        mgr = AccountManager()
        with pytest.raises(ValueError, match="Duplicate account name"):
            mgr.load_config(p)

    def test_unknown_account_lookup_raises(self, tmp_path):
        p = _write(tmp_path, {"accounts": [
            {"name": "only", "imap": {}, "credentials": {},
             "cache": {"enabled": False}},
        ]})
        mgr = AccountManager()
        mgr.load_config(p)
        with pytest.raises(ValueError, match="Unknown account"):
            mgr.resolve_name("does-not-exist")

    def test_per_account_auto_archive_overrides_top_level(self, tmp_path):
        p = _write(tmp_path, {
            "auto_archive": {"senders_file": "shared.json"},
            "accounts": [
                {"name": "a", "default": True, "imap": {}, "credentials": {},
                 "cache": {"enabled": False},
                 "auto_archive": {"senders_file": "a-senders.json"}},
                {"name": "b", "imap": {}, "credentials": {},
                 "cache": {"enabled": False}},
            ]
        })
        mgr = AccountManager()
        mgr.load_config(p)
        assert mgr.accounts["a"].config["auto_archive"]["senders_file"] == "a-senders.json"
        # b inherits the shared one.
        assert mgr.accounts["b"].config["auto_archive"]["senders_file"] == "shared.json"


class TestMigration:
    def test_migrate_writes_backup_and_new_format(self, tmp_path):
        legacy = {"imap": {"host": "h"}, "credentials": {"username": "u"}}
        p = _write(tmp_path, legacy)
        backup = migrate_legacy_config(p)

        new = json.loads(open(p).read())
        assert "accounts" in new
        assert new["accounts"][0]["name"] == "default"
        assert new["accounts"][0]["default"] is True
        assert new["accounts"][0]["imap"]["host"] == "h"

        old = json.loads(open(backup).read())
        assert old == legacy

    def test_migrate_already_new_format_rejects(self, tmp_path):
        p = _write(tmp_path, {"accounts": [{"name": "a"}]})
        with pytest.raises(ValueError, match="already in new format"):
            migrate_legacy_config(p)
