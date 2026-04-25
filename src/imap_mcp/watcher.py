"""IMAP IDLE watcher -- permanent background watcher for mailboxes.

Runs one daemon thread per watched folder, using IMAP IDLE to detect
changes and refreshing an in-memory cache when new mail arrives.
"""

import json
import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Callable
from dataclasses import dataclass, field

from imapclient import IMAPClient

logger = logging.getLogger(__name__)


@dataclass
class EmailSummary:
    """Lightweight email summary for cache."""
    uid: int
    sender: str
    sender_name: Optional[str]
    subject: str
    date: datetime
    unread: bool


@dataclass
class MailboxCache:
    """Cache for a single mailbox."""
    name: str
    emails: list[EmailSummary] = field(default_factory=list)
    total: int = 0
    unread: int = 0
    last_updated: Optional[datetime] = None


class ImapWatcher:
    """Permanent IMAP IDLE watcher with in-memory cache."""

    def __init__(self, config_path: str = "config.json"):
        self.config_path = config_path
        self.config: dict = {}
        self.cache: dict[str, MailboxCache] = {}
        self.watch_threads: dict[str, threading.Thread] = {}
        self.stop_events: dict[str, threading.Event] = {}
        self.running = False
        self.on_update: Optional[Callable[[str, MailboxCache], None]] = None
        self._lock = threading.Lock()

    def load_config(self) -> dict:
        """Load configuration."""
        path = Path(self.config_path)
        if not path.is_absolute():
            # Resolve relative to project root (two levels up from this module)
            project_root = Path(__file__).parent.parent.parent
            path = project_root / self.config_path
        if path.exists():
            with open(path) as f:
                self.config = json.load(f)
        return self.config

    def _create_connection(self) -> IMAPClient:
        """Create a new IMAP connection."""
        from .imap_client import get_stored_password

        imap_config = self.config.get("imap", {})
        creds = self.config.get("credentials", {})

        username = creds.get("username", "")
        password = creds.get("password", "")
        if not password and username:
            password = get_stored_password(username) or ""

        client = IMAPClient(
            imap_config.get("host"),
            port=imap_config.get("port", 993),
            ssl=imap_config.get("secure", True),
        )
        client.login(username, password)
        return client

    def _get_watched_folders(self) -> dict[str, str]:
        """Get folders to watch."""
        folders = self.config.get("folders", {})
        return {
            "inbox": folders.get("inbox", "INBOX"),
            "next": folders.get("next", "next"),
            "waiting": folders.get("waiting", "waiting"),
            "someday": folders.get("someday", "someday"),
        }

    def _fetch_mailbox_summary(self, client: IMAPClient, folder: str) -> MailboxCache:
        """Fetch email summaries for a mailbox."""
        try:
            result = client.select_folder(folder)
            total = result.get(b"EXISTS", 0)

            # Get status for unread count
            status = client.folder_status(folder, ["UNSEEN"])
            unread = status.get(b"UNSEEN", 0)

            # Fetch latest 50 emails
            if total > 0:
                uids = client.search(["ALL"])
                uids = sorted(uids, reverse=True)[:50]

                if uids:
                    messages = client.fetch(uids, ["ENVELOPE", "FLAGS"])
                    emails = []

                    for uid, data in messages.items():
                        envelope = data.get(b"ENVELOPE")
                        flags = data.get(b"FLAGS", [])

                        if envelope:
                            # Extract sender
                            sender_email = ""
                            sender_name = None
                            if envelope.from_:
                                f = envelope.from_[0]
                                mailbox = f.mailbox.decode() if f.mailbox else ""
                                host = f.host.decode() if f.host else ""
                                sender_email = f"{mailbox}@{host}"
                                if f.name:
                                    try:
                                        sender_name = f.name.decode("utf-8", errors="replace")
                                    except Exception:
                                        sender_name = str(f.name)

                            # Extract subject
                            subject = ""
                            if envelope.subject:
                                try:
                                    subject = envelope.subject.decode("utf-8", errors="replace")
                                except Exception:
                                    subject = str(envelope.subject)

                            # Check if unread
                            is_unread = b"\\Seen" not in flags

                            emails.append(EmailSummary(
                                uid=uid,
                                sender=sender_email,
                                sender_name=sender_name,
                                subject=subject,
                                date=envelope.date or datetime.now(),
                                unread=is_unread,
                            ))

                    return MailboxCache(
                        name=folder,
                        emails=sorted(emails, key=lambda e: e.date, reverse=True),
                        total=total,
                        unread=unread,
                        last_updated=datetime.now(),
                    )

            return MailboxCache(
                name=folder,
                emails=[],
                total=total,
                unread=unread,
                last_updated=datetime.now(),
            )

        except Exception as e:
            logger.warning("Error fetching %s: %s", folder, e)
            return MailboxCache(name=folder, last_updated=datetime.now())

    def _watch_folder(self, key: str, folder: str, stop_event: threading.Event) -> None:
        """Watch a single folder via IMAP IDLE, refreshing cache on changes."""
        while not stop_event.is_set():
            client = None
            try:
                client = self._create_connection()
                client.select_folder(folder)

                # Initial fetch
                cache = self._fetch_mailbox_summary(client, folder)
                with self._lock:
                    self.cache[key] = cache
                if self.on_update:
                    self.on_update(key, cache)

                # IDLE loop
                while not stop_event.is_set():
                    client.idle()

                    # Wait for events (30 second timeout for keepalive)
                    responses = client.idle_check(timeout=30)
                    client.idle_done()

                    if responses:
                        # Something changed, refresh cache
                        cache = self._fetch_mailbox_summary(client, folder)
                        with self._lock:
                            self.cache[key] = cache
                        if self.on_update:
                            self.on_update(key, cache)

            except Exception as e:
                logger.warning("Watcher error for %s: %s", folder, e)
                time.sleep(5)  # Wait before reconnect

            finally:
                if client:
                    try:
                        client.logout()
                    except Exception:
                        pass

    def start(self) -> None:
        """Start watching all configured folders in background threads."""
        if self.running:
            return

        self.load_config()
        self.running = True
        folders = self._get_watched_folders()

        for key, folder in folders.items():
            stop_event = threading.Event()
            self.stop_events[key] = stop_event

            thread = threading.Thread(
                target=self._watch_folder,
                args=(key, folder, stop_event),
                daemon=True,
                name=f"imap-watcher-{key}",
            )
            self.watch_threads[key] = thread
            thread.start()

        logger.info("Started watching %d folders: %s", len(folders), list(folders.keys()))

    def stop(self) -> None:
        """Stop all watcher threads and wait for them to finish."""
        if not self.running:
            return

        self.running = False

        # Signal all threads to stop
        for stop_event in self.stop_events.values():
            stop_event.set()

        # Wait for threads to finish
        for key, thread in self.watch_threads.items():
            thread.join(timeout=5)

        self.watch_threads.clear()
        self.stop_events.clear()
        logger.info("Stopped all watchers")

    def get_cache(self, key: Optional[str] = None) -> dict:
        """Get cached data. Always returns dict with mailbox keys."""
        with self._lock:
            if key:
                cache = self.cache.get(key)
                if cache:
                    return {key: self._cache_to_dict(cache)}
                return {}

            return {k: self._cache_to_dict(v) for k, v in self.cache.items()}

    def _cache_to_dict(self, cache: MailboxCache) -> dict:
        """Convert cache to dictionary."""
        return {
            "name": cache.name,
            "emails": [
                {
                    "uid": e.uid,
                    "sender": e.sender,
                    "sender_name": e.sender_name,
                    "subject": e.subject,
                    "date": e.date.isoformat() if e.date else None,
                    "unread": e.unread,
                }
                for e in cache.emails
            ],
            "total": cache.total,
            "unread": cache.unread,
            "last_updated": cache.last_updated.isoformat() if cache.last_updated else None,
        }

    def refresh(self, key: Optional[str] = None) -> None:
        """Force-refresh the cache for one or all watched folders."""
        folders = self._get_watched_folders()

        if key and key in folders:
            folders = {key: folders[key]}

        client = self._create_connection()
        try:
            for k, folder in folders.items():
                cache = self._fetch_mailbox_summary(client, folder)
                with self._lock:
                    self.cache[k] = cache
        finally:
            client.logout()


# Global watcher instance
_watcher: Optional[ImapWatcher] = None


def get_watcher(config_path: str = "config.json") -> ImapWatcher:
    """Get or create global watcher instance."""
    global _watcher
    if _watcher is None:
        _watcher = ImapWatcher(config_path)
    return _watcher
