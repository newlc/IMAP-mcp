"""Multi-account configuration and lifecycle management.

The new config format wraps every account in an ``accounts`` array:

.. code-block:: json

    {
      "accounts": [
        {
          "name": "work",
          "default": true,
          "imap": {...},
          "smtp": {...},
          "credentials": {"username": "..."},
          "user": {...},
          "folders": {...},
          "cache": {"enabled": true, "encrypt": true,
                    "db_path": "~/.imap-mcp/work.db"}
        }
      ]
    }

Each account gets its own :class:`~imap_mcp.imap_client.ImapClientWrapper`,
its own :class:`~imap_mcp.cache.EmailCache` (with an isolated keyring entry
for the encryption key) and, optionally, its own IDLE watcher.

Connections are opened lazily on the first tool call for an account, except
when ``cache.enabled = true`` -- in that case a watcher is started eagerly
during :meth:`AccountManager.load_config`, which itself opens the IMAP socket.
"""

from __future__ import annotations

import json
import logging
import random
import threading
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# How often, by default, to NOOP the main IMAP socket to keep it alive.
# IMAP IDLE timeout per RFC 2177 is "no longer than 29 minutes"; many
# servers also drop non-IDLE idle sockets after ~30 minutes. 20 minutes
# leaves comfortable margin while not flooding the server.
KEEPALIVE_INTERVAL_SECS = 20 * 60

# Top-level config keys that look like single-account settings. If any of
# these are present at the root and "accounts" is missing, we refuse to load
# and ask the user to migrate.
_LEGACY_TOP_LEVEL_KEYS = {"imap", "smtp", "credentials", "user", "folders"}


class Account:
    """One configured email account: wrapper + per-account state.

    A re-entrant lock guards :meth:`ensure_connected` and :meth:`reconnect`
    so two concurrent tool calls or a watcher reconnect can't race to open
    duplicate sockets. A daemon keepalive thread pings the main IMAP
    socket every ``KEEPALIVE_INTERVAL_SECS`` so the server doesn't drop
    it after the typical 30-minute idle timeout.
    """

    def __init__(self, name: str, config: dict):
        # Lazily import to avoid circular import (imap_client imports accounts
        # only at runtime via AccountManager).
        from .imap_client import ImapClientWrapper

        self.name = name
        self.config = config
        self.client = ImapClientWrapper()
        self.client.config = config
        self.client.account_name = name
        self._connected = False
        self._lock = threading.RLock()
        self._keepalive_thread: Optional[threading.Thread] = None
        self._keepalive_stop: Optional[threading.Event] = None

    def ensure_connected(self):
        """Open the IMAP socket and initialize cache/watcher on first use.

        Holding ``self._lock`` makes this safe under concurrent callers:
        the second caller waits until the first finishes the handshake
        and then reuses the same connection.
        """
        with self._lock:
            if self._connected and self.client.client is not None:
                return self.client
            self.client._connect_with_loaded_config()
            self._connected = True
            self._start_keepalive()
            return self.client

    def reconnect(self):
        """Force-close the current socket and reopen a fresh one."""
        with self._lock:
            try:
                self.client.disconnect()
            except Exception as exc:  # noqa: BLE001
                logger.debug("disconnect() during reconnect failed: %s", exc)
            self._connected = False
            return self.ensure_connected()

    def disconnect(self) -> None:
        with self._lock:
            self._stop_keepalive()
            if not self._connected:
                return
            try:
                self.client.disconnect()
            finally:
                self._connected = False

    def _start_keepalive(self) -> None:
        """Spawn the keepalive thread once. No-op if already running."""
        if self._keepalive_thread and self._keepalive_thread.is_alive():
            return
        self._keepalive_stop = threading.Event()
        self._keepalive_thread = threading.Thread(
            target=self._keepalive_loop,
            name=f"imap-keepalive-{self.name}",
            daemon=True,
        )
        self._keepalive_thread.start()

    def _stop_keepalive(self) -> None:
        if self._keepalive_stop is not None:
            self._keepalive_stop.set()
        self._keepalive_thread = None
        self._keepalive_stop = None

    def _keepalive_loop(self) -> None:
        """Send a NOOP every KEEPALIVE_INTERVAL_SECS; reconnect on failure.

        Adds a small jitter on the first wait so multiple accounts started
        together don't all NOOP at the same instant.
        """
        # Initial jitter: 0-30s.
        first_wait = random.uniform(0, 30)
        if self._keepalive_stop and self._keepalive_stop.wait(first_wait):
            return
        while self._keepalive_stop and not self._keepalive_stop.is_set():
            try:
                with self._lock:
                    if not self._connected or self.client.client is None:
                        break
                    self.client.client.noop()
            except Exception as exc:  # noqa: BLE001
                logger.info(
                    "Keepalive NOOP failed for %s (%s); reconnecting.",
                    self.name, exc,
                )
                try:
                    self.reconnect()
                except Exception as reconn_exc:
                    logger.warning(
                        "Keepalive reconnect failed for %s: %s",
                        self.name, reconn_exc,
                    )
            # Sleep with jitter (±10%) until next NOOP.
            wait = KEEPALIVE_INTERVAL_SECS * random.uniform(0.9, 1.1)
            if self._keepalive_stop.wait(wait):
                return

    def info(self) -> dict:
        creds = self.config.get("credentials", {})
        cache_cfg = self.config.get("cache", {})
        return {
            "name": self.name,
            "username": creds.get("username", ""),
            "imap_host": self.config.get("imap", {}).get("host"),
            "smtp_host": self.config.get("smtp", {}).get("host"),
            "cache_enabled": cache_cfg.get("enabled", False),
            "cache_encrypted": cache_cfg.get("encrypt", False),
            "cache_db_path": cache_cfg.get("db_path"),
            "connected": self._connected,
        }


class AccountManager:
    """Holds every configured account; routes tool calls by account name."""

    def __init__(self):
        self.accounts: dict[str, Account] = {}
        self.default_name: Optional[str] = None
        self.config_path: Optional[str] = None

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    @staticmethod
    def is_legacy_config(raw: dict) -> bool:
        """Return True if the dict looks like the old single-account format."""
        return "accounts" not in raw and any(k in raw for k in _LEGACY_TOP_LEVEL_KEYS)

    @staticmethod
    def wrap_legacy_config(raw: dict) -> dict:
        """Convert a legacy single-account config dict into the new format."""
        account = {k: raw[k] for k in raw if not k.startswith("_")}
        # Promote it into a single-account "accounts" list.
        account.setdefault("name", "default")
        return {"accounts": [account]}

    def load_config(self, config_path: str) -> None:
        """Load and validate the config file, registering every account.

        Eagerly starts IDLE watchers for accounts with ``cache.enabled = true``
        (this opens their IMAP socket as a side effect). Other accounts stay
        disconnected until :meth:`get` is first called for them.
        """
        path = Path(config_path)
        if not path.is_absolute():
            project_root = Path(__file__).parent.parent.parent
            path = project_root / config_path
        if not path.exists():
            raise FileNotFoundError(f"Config file not found: {path}")

        with open(path) as f:
            raw = json.load(f)

        if "accounts" not in raw:
            if self.is_legacy_config(raw):
                raise ValueError(
                    "config.json uses the legacy single-account format. "
                    "Run 'imap-mcp --migrate-config --config <path>' to "
                    "rewrite it as a multi-account config."
                )
            raise ValueError("config.json: missing required 'accounts' array.")

        accounts_data = raw["accounts"]
        if not isinstance(accounts_data, list) or not accounts_data:
            raise ValueError("config.accounts must be a non-empty array.")

        self.config_path = str(path)
        defaults: list[str] = []

        for raw_account in accounts_data:
            if not isinstance(raw_account, dict):
                raise ValueError("Each entry of config.accounts must be an object.")
            name = raw_account.get("name")
            if not name or not isinstance(name, str):
                raise ValueError("Each account must have a non-empty 'name' string.")
            if name in self.accounts:
                raise ValueError(f"Duplicate account name in config: {name!r}")

            # Apply per-account defaults so each cache lives in its own file
            # and uses its own keyring entry for the encryption key.
            cache_cfg = raw_account.setdefault("cache", {})
            cache_cfg.setdefault("db_path", f"~/.imap-mcp/{name}.db")
            cache_cfg.setdefault("encrypt", False)
            cache_cfg.setdefault("keyring_username", f"encryption-key-{name}")

            # Auto-archive: per-account block falls back to top-level (shared).
            if "auto_archive" not in raw_account and "auto_archive" in raw:
                raw_account["auto_archive"] = raw["auto_archive"]

            # Stash the path so the watcher (which currently re-reads the
            # config to find folders) keeps working.
            raw_account["_config_path"] = str(path)
            raw_account["_account_name"] = name

            self.accounts[name] = Account(name, raw_account)
            if raw_account.get("default") is True:
                defaults.append(name)

        if len(defaults) > 1:
            raise ValueError(
                f"More than one account marked default: {defaults}. "
                "Mark exactly one with default: true."
            )
        if len(defaults) == 1:
            self.default_name = defaults[0]
        elif len(self.accounts) == 1:
            self.default_name = next(iter(self.accounts))
        else:
            raise ValueError(
                f"Multiple accounts configured ({list(self.accounts)}) but none "
                "marked 'default: true'. Add 'default: true' to exactly one."
            )

        # Eagerly start watchers for accounts that asked for one. This also
        # opens their IMAP socket. Failures are logged but don't abort the
        # server -- other accounts may still work.
        for acct in self.accounts.values():
            if acct.config.get("cache", {}).get("enabled", False):
                try:
                    acct.ensure_connected()
                except Exception as exc:
                    logger.warning(
                        "Failed to auto-connect account %r: %s", acct.name, exc
                    )

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------

    def has_accounts(self) -> bool:
        return bool(self.accounts)

    def resolve_name(self, name: Optional[str] = None) -> str:
        if name is None:
            if not self.default_name:
                raise RuntimeError(
                    "No accounts loaded. Call auto_connect first."
                )
            return self.default_name
        if name not in self.accounts:
            raise ValueError(
                f"Unknown account: {name!r}. Known accounts: "
                f"{sorted(self.accounts)}"
            )
        return name

    def get(self, name: Optional[str] = None):
        """Return the (lazily connected) wrapper for account ``name``."""
        resolved = self.resolve_name(name)
        return self.accounts[resolved].ensure_connected()

    def get_account(self, name: Optional[str] = None) -> Account:
        return self.accounts[self.resolve_name(name)]

    def list_accounts(self) -> list[dict]:
        return [
            {**acct.info(), "default": acct.name == self.default_name}
            for acct in self.accounts.values()
        ]

    def disconnect_all(self) -> None:
        for acct in list(self.accounts.values()):
            try:
                acct.disconnect()
            except Exception as exc:
                logger.warning("Error disconnecting %r: %s", acct.name, exc)


# ---------------------------------------------------------------------------
# Migration helper
# ---------------------------------------------------------------------------


def migrate_legacy_config(config_path: str) -> str:
    """Rewrite an old single-account ``config.json`` into the new format.

    Returns the path of the backup file. The original file is left in place
    as ``<path>.bak`` and the new format is written to ``<path>``.
    """
    path = Path(config_path)
    if not path.is_absolute():
        project_root = Path(__file__).parent.parent.parent
        path = project_root / config_path
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")

    with open(path) as f:
        raw = json.load(f)

    if "accounts" in raw:
        raise ValueError(f"Config already in new format: {path}")
    if not AccountManager.is_legacy_config(raw):
        raise ValueError(
            f"Config doesn't look like a legacy single-account file: {path}"
        )

    backup_path = str(path) + ".bak"
    with open(backup_path, "w") as f:
        json.dump(raw, f, indent=2, ensure_ascii=False)

    new_config = AccountManager.wrap_legacy_config(raw)
    new_config["accounts"][0].setdefault("default", True)

    with open(path, "w") as f:
        json.dump(new_config, f, indent=2, ensure_ascii=False)

    return backup_path
