"""IMAP client wrapper for MCP Server.

Provides high-level operations (fetch, search, move, cache, auto-archive)
on top of ``imapclient.IMAPClient``, with transparent persistent caching
via :class:`~imap_mcp.cache.EmailCache`.
"""

import base64
import email
import email.header
import email.utils
import json
import logging
import mimetypes
import shutil
import smtplib
import ssl
import tempfile
import time
from datetime import datetime
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional

import keyring
from imapclient import IMAPClient

logger_imap = logging.getLogger(__name__)


def _normalize_delimiter(raw) -> str:
    """Coerce an IMAP folder delimiter (bytes/str/None) into a non-empty str.

    Some servers return ``None`` for the LIST/LSUB delimiter (especially
    for the root namespace). MailboxInfo.delimiter is required, so default
    to ``'/'`` -- it's the most common and the namespace-aware code paths
    don't depend on the exact value here.
    """
    if raw is None:
        return "/"
    if isinstance(raw, bytes):
        try:
            return raw.decode("utf-8") or "/"
        except UnicodeDecodeError:
            return "/"
    return str(raw) or "/"


def _is_inside(child: Path, parent: Path) -> bool:
    """Return True if ``child`` is the same as or located under ``parent``.

    Both paths must already be resolved (real paths) for the symlink check
    to be meaningful.
    """
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False

from .models import (
    EmailAddress,
    EmailHeader,
    EmailBody,
    Email,
    Attachment,
    MailboxStatus,
    MailboxInfo,
    AutoArchiveSender,
)
from .cache import EmailCache
from .mail_utils import (
    extract_inline_images,
    html_to_plain,
    inline_cid_to_data_uri,
    parse_authentication_results,
    parse_calendar_invites,
    sanitize_html,
    with_retries,
)
from .watcher import ImapWatcher

KEYRING_SERVICE = "imap-mcp"


def get_stored_password(username: str) -> Optional[str]:
    """Retrieve password from the OS keyring."""
    return keyring.get_password(KEYRING_SERVICE, username)


def store_password(username: str, password: str) -> None:
    """Store password in the OS keyring."""
    keyring.set_password(KEYRING_SERVICE, username, password)


def delete_stored_password(username: str) -> None:
    """Delete password from the OS keyring."""
    try:
        keyring.delete_password(KEYRING_SERVICE, username)
    except keyring.errors.PasswordDeleteError:
        pass


class ImapClientWrapper:
    """Wrapper around IMAPClient for MCP operations.

    Each instance represents one email account. In multi-account mode the
    :class:`~imap_mcp.accounts.Account` owning this wrapper sets ``config``
    directly and calls :meth:`_connect_with_loaded_config` instead of
    :meth:`auto_connect`.
    """

    def __init__(self):
        self.client: Optional[IMAPClient] = None
        self.config: dict = {}
        self.account_name: Optional[str] = None
        self.current_mailbox: Optional[str] = None
        self.cache: dict = {}
        self.cache_timestamps: dict = {}
        self.auto_archive_senders: list[AutoArchiveSender] = []
        self.watching: bool = False
        self.watcher: Optional[ImapWatcher] = None
        self.email_cache: Optional[EmailCache] = None

    def load_config(self, config_path: str = "config.json") -> dict:
        """Load configuration from JSON file."""
        path = Path(config_path)
        if not path.is_absolute():
            # Resolve relative to project root (two levels up from this module)
            project_root = Path(__file__).parent.parent.parent
            path = project_root / config_path
        if not path.exists():
            raise FileNotFoundError(f"Config file not found: {path}")
        with open(path) as f:
            self.config = json.load(f)
        return self.config

    def connect(self, host: str, port: int = 993, secure: bool = True) -> bool:
        """Establish IMAP connection. Retries on transient network errors."""
        self.client = with_retries(
            lambda: IMAPClient(host, port=port, ssl=secure)
        )
        return True

    def authenticate(self, username: str, password: str) -> bool:
        """Login with username and password."""
        if not self.client:
            raise RuntimeError("Not connected. Call connect() first.")
        self.client.login(username, password)
        return True

    def disconnect(self) -> bool:
        """Close IMAP connection."""
        # Stop watcher if running
        if self.watcher and self.watching:
            self.watcher.stop()
            self.watching = False

        if self.email_cache:
            self.email_cache.close()
            self.email_cache = None

        if self.client:
            try:
                self.client.logout()
            except Exception:
                pass
            self.client = None
            self.current_mailbox = None
        return True

    def auto_connect(self, config_path: str = "config.json") -> bool:
        """Connect using config.json credentials.

        Password resolution order:
        1. ``credentials.password`` in config.json (if non-empty)
        2. OS keyring (macOS Keychain / Windows Credential Locker / Linux SecretService)
        """
        self.load_config(config_path)
        self.config["_config_path"] = config_path  # Store for watcher
        return self._connect_with_loaded_config()

    def _connect_with_loaded_config(self) -> bool:
        """Open the IMAP connection using ``self.config``.

        Used by :class:`~imap_mcp.accounts.Account` (multi-account mode)
        which sets ``self.config`` directly without going through
        :meth:`load_config`.
        """
        imap_config = self.config.get("imap", {})
        creds = self.config.get("credentials", {})

        username = creds.get("username", "")
        password = creds.get("password", "")

        if not password and username:
            password = get_stored_password(username)
            if not password:
                raise RuntimeError(
                    f"No password found for {username}. "
                    "Set it with: imap-mcp --set-password"
                )

        self.connect(
            host=imap_config.get("host"),
            port=imap_config.get("port", 993),
            secure=imap_config.get("secure", True),
        )
        self.authenticate(username, password)
        self._load_auto_archive_config()

        cache_config = self.config.get("cache", {})
        db_path = cache_config.get("db_path", "~/.imap-mcp/cache.db")
        encrypt = cache_config.get("encrypt", False)
        keyring_username = cache_config.get(
            "keyring_username",
            f"encryption-key-{self.account_name}" if self.account_name else "encryption-key",
        )
        self.email_cache = EmailCache(
            db_path, encrypted=encrypt, keyring_username=keyring_username
        )

        if cache_config.get("enabled", True):
            # Construct a watcher tied to this account's config dict directly
            # so multi-account setups don't share a single global watcher.
            self.watcher = ImapWatcher(
                config_path=self.config.get("_config_path"),
                config=self.config,
            )
            self.watcher.start()
            self.watching = True

        return True

    def _load_auto_archive_config(self):
        """Load auto-archive sender list."""
        aa_config = self.config.get("auto_archive", {})
        if not aa_config.get("enabled", False):
            return

        senders_file = aa_config.get("senders_file", "auto_archive_senders.json")
        path = Path(senders_file)
        if path.exists():
            with open(path) as f:
                data = json.load(f)
                self.auto_archive_senders = [
                    AutoArchiveSender(**s) for s in data.get("senders", [])
                ]

    def _ensure_connected(self):
        """Ensure client is connected."""
        if not self.client:
            raise RuntimeError("Not connected. Call connect() or auto_connect() first.")

    @staticmethod
    def _to_imap_date(date_str: str) -> str:
        """Convert ISO date (2026-04-24) to IMAP format (24-Apr-2026)."""
        if not date_str:
            return date_str
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d")
            return dt.strftime("%d-%b-%Y")
        except ValueError:
            return date_str  # already in correct format or other format

    # === Mailbox Operations ===

    def list_mailboxes(
        self, pattern: str = "*",
        cursor: int = 0, limit: Optional[int] = None,
    ) -> dict:
        """List all mailbox folders, optionally paginated.

        Returns ``{"mailboxes": [...], "total": N, "next_cursor": K | None}``.
        On servers with thousands of folders pass ``limit`` and use
        ``next_cursor`` to page through them.
        """
        self._ensure_connected()
        folders = self.client.list_folders(pattern=pattern)
        all_mailboxes = [
            MailboxInfo(
                name=(f[2].decode() if isinstance(f[2], bytes) else f[2]),
                delimiter=_normalize_delimiter(f[1]),
                flags=[
                    (fl.decode() if isinstance(fl, bytes) else str(fl))
                    for fl in f[0]
                ],
            )
            for f in folders
        ]
        total = len(all_mailboxes)
        if limit is None:
            return {"mailboxes": all_mailboxes, "total": total, "next_cursor": None}
        if cursor < 0:
            raise ValueError("cursor must be >= 0")
        if limit <= 0:
            raise ValueError("limit must be > 0")
        end = cursor + limit
        page = all_mailboxes[cursor:end]
        next_cursor: Optional[int] = end if end < total else None
        return {"mailboxes": page, "total": total, "next_cursor": next_cursor}

    def select_mailbox(self, mailbox: str) -> MailboxStatus:
        """Select/open a mailbox folder.

        Automatically retries with ``INBOX.`` prefix if the server
        requires namespaced folder names.
        """
        self._ensure_connected()
        try:
            result = self.client.select_folder(mailbox)
        except Exception as exc:
            if not mailbox.startswith("INBOX.") and mailbox != "INBOX":
                result = self.client.select_folder(f"INBOX.{mailbox}")
                mailbox = f"INBOX.{mailbox}"
            else:
                raise
        self.current_mailbox = mailbox
        return MailboxStatus(
            name=mailbox,
            exists=self._parse_status_value(result.get(b"EXISTS", 0)),
            recent=self._parse_status_value(result.get(b"RECENT", 0)),
            unseen=self._parse_status_value(result.get(b"UNSEEN", 0)) if b"UNSEEN" in result else 0,
            uidnext=self._parse_status_value(result.get(b"UIDNEXT", 0)),
            uidvalidity=self._parse_status_value(result.get(b"UIDVALIDITY", 0)),
        )

    def create_mailbox(self, mailbox: str) -> bool:
        """Create a new mailbox folder."""
        self._ensure_connected()
        try:
            self.client.create_folder(mailbox)
        except Exception as e:
            # Retry with INBOX. namespace prefix (e.g. Jino servers)
            if "namespace" in str(e).lower() or "no such" in str(e).lower() or "mailbox" in str(e).lower():
                prefixed = f"INBOX.{mailbox}"
                self.client.create_folder(prefixed)
            else:
                raise
        return True

    def rename_mailbox(self, old_name: str, new_name: str) -> dict:
        """Rename ``old_name`` to ``new_name``."""
        self._ensure_connected()
        try:
            self.client.rename_folder(old_name, new_name)
            actual_old, actual_new = old_name, new_name
        except Exception as exc:
            err = str(exc).lower()
            if "namespace" in err or "no such" in err or "mailbox" in err:
                actual_old = old_name if old_name.startswith("INBOX.") else f"INBOX.{old_name}"
                actual_new = new_name if new_name.startswith("INBOX.") else f"INBOX.{new_name}"
                self.client.rename_folder(actual_old, actual_new)
            else:
                raise
        return {"renamed": True, "from": actual_old, "to": actual_new}

    def delete_mailbox(self, mailbox: str) -> dict:
        """Delete a mailbox folder. Server may refuse to delete non-empty folders."""
        self._ensure_connected()
        try:
            self.client.delete_folder(mailbox)
            actual = mailbox
        except Exception as exc:
            err = str(exc).lower()
            if (
                ("namespace" in err or "no such" in err or "mailbox" in err)
                and not mailbox.startswith("INBOX.")
                and mailbox != "INBOX"
            ):
                actual = f"INBOX.{mailbox}"
                self.client.delete_folder(actual)
            else:
                raise
        return {"deleted": True, "mailbox": actual}

    def empty_mailbox(self, mailbox: str) -> dict:
        """Delete every message in ``mailbox`` (\\Deleted + EXPUNGE)."""
        self._ensure_connected()
        self.select_mailbox(mailbox)
        uids = self.client.search(["ALL"])
        if not uids:
            return {"emptied": True, "mailbox": self.current_mailbox, "deleted_count": 0}
        self.client.add_flags(uids, [b"\\Deleted"])
        self.client.expunge()
        return {
            "emptied": True,
            "mailbox": self.current_mailbox,
            "deleted_count": len(uids),
        }

    def subscribe_mailbox(self, mailbox: str) -> dict:
        """Add ``mailbox`` to the subscribed list."""
        self._ensure_connected()
        try:
            self.client.subscribe_folder(mailbox)
            actual = mailbox
        except Exception as exc:
            err = str(exc).lower()
            if (
                ("namespace" in err or "no such" in err or "mailbox" in err)
                and not mailbox.startswith("INBOX.")
                and mailbox != "INBOX"
            ):
                actual = f"INBOX.{mailbox}"
                self.client.subscribe_folder(actual)
            else:
                raise
        return {"subscribed": True, "mailbox": actual}

    def unsubscribe_mailbox(self, mailbox: str) -> dict:
        """Remove ``mailbox`` from the subscribed list."""
        self._ensure_connected()
        try:
            self.client.unsubscribe_folder(mailbox)
            actual = mailbox
        except Exception as exc:
            err = str(exc).lower()
            if (
                ("namespace" in err or "no such" in err or "mailbox" in err)
                and not mailbox.startswith("INBOX.")
                and mailbox != "INBOX"
            ):
                actual = f"INBOX.{mailbox}"
                self.client.unsubscribe_folder(actual)
            else:
                raise
        return {"unsubscribed": True, "mailbox": actual}

    def list_subscribed_mailboxes(self, pattern: str = "*") -> list[MailboxInfo]:
        """List subscribed mailboxes (matches the IMAP LSUB command)."""
        self._ensure_connected()
        folders = self.client.list_sub_folders(pattern=pattern)
        return [
            MailboxInfo(
                name=f[2],
                delimiter=(f[1].decode() if isinstance(f[1], bytes) else f[1]) or "/",
                flags=[fl.decode() if isinstance(fl, bytes) else str(fl) for fl in f[0]],
            )
            for f in folders
        ]

    @staticmethod
    def _parse_status_value(value) -> int:
        """Parse IMAP status value to int.

        Some servers return values as lists (e.g. [b'44831']) instead of ints.
        """
        if isinstance(value, int):
            return value
        if isinstance(value, (list, tuple)) and value:
            value = value[0]
        if isinstance(value, bytes):
            return int(value)
        return int(value)

    def get_mailbox_status(self, mailbox: str) -> MailboxStatus:
        """Get mailbox status (message count, unseen, etc.)."""
        self._ensure_connected()
        status = self.client.folder_status(
            mailbox, ["MESSAGES", "RECENT", "UNSEEN", "UIDNEXT", "UIDVALIDITY"]
        )
        return MailboxStatus(
            name=mailbox,
            exists=self._parse_status_value(status.get(b"MESSAGES", 0)),
            recent=self._parse_status_value(status.get(b"RECENT", 0)),
            unseen=self._parse_status_value(status.get(b"UNSEEN", 0)),
            uidnext=self._parse_status_value(status.get(b"UIDNEXT", 0)),
            uidvalidity=self._parse_status_value(status.get(b"UIDVALIDITY", 0)),
        )

    # === Email Reading ===

    def _parse_address(self, addr) -> Optional[EmailAddress]:
        """Parse email address from header."""
        if not addr:
            return None
        if isinstance(addr, tuple):
            name, email_addr = addr
            return EmailAddress(name=name, email=email_addr or "")
        return EmailAddress(email=str(addr))

    def _parse_addresses(self, addrs) -> list[EmailAddress]:
        """Parse multiple email addresses."""
        if not addrs:
            return []
        if isinstance(addrs, str):
            parsed = email.utils.getaddresses([addrs])
            return [EmailAddress(name=n or None, email=e) for n, e in parsed if e]
        return [self._parse_address(a) for a in addrs if a]

    def _decode_header(self, header_value) -> str:
        """Decode email header value."""
        if not header_value:
            return ""
        if isinstance(header_value, bytes):
            header_value = header_value.decode("utf-8", errors="replace")
        decoded_parts = email.header.decode_header(header_value)
        result = []
        for part, charset in decoded_parts:
            if isinstance(part, bytes):
                charset = charset or "utf-8"
                try:
                    result.append(part.decode(charset, errors="replace"))
                except (LookupError, UnicodeDecodeError):
                    result.append(part.decode("utf-8", errors="replace"))
            else:
                result.append(str(part))
        return "".join(result)

    def _parse_email_header(self, uid: int, data: dict) -> EmailHeader:
        """Parse email header from IMAP response."""
        envelope = data.get(b"ENVELOPE")
        flags = [f.decode() if isinstance(f, bytes) else str(f)
                 for f in data.get(b"FLAGS", [])]
        size = data.get(b"RFC822.SIZE", 0)

        if envelope:
            date = envelope.date
            subject = self._decode_header(envelope.subject) if envelope.subject else None
            from_addr = None
            if envelope.from_:
                f = envelope.from_[0]
                from_addr = EmailAddress(
                    name=self._decode_header(f.name) if f.name else None,
                    email=f"{f.mailbox.decode() if f.mailbox else ''}@{f.host.decode() if f.host else ''}"
                )
            to_addrs = []
            if envelope.to:
                for t in envelope.to:
                    to_addrs.append(EmailAddress(
                        name=self._decode_header(t.name) if t.name else None,
                        email=f"{t.mailbox.decode() if t.mailbox else ''}@{t.host.decode() if t.host else ''}"
                    ))
            cc_addrs = []
            if envelope.cc:
                for c in envelope.cc:
                    cc_addrs.append(EmailAddress(
                        name=self._decode_header(c.name) if c.name else None,
                        email=f"{c.mailbox.decode() if c.mailbox else ''}@{c.host.decode() if c.host else ''}"
                    ))
            message_id = envelope.message_id.decode() if envelope.message_id else None
        else:
            date = None
            subject = None
            from_addr = None
            to_addrs = []
            cc_addrs = []
            message_id = None

        return EmailHeader(
            uid=uid,
            message_id=message_id,
            subject=subject,
            from_address=from_addr,
            to_addresses=to_addrs,
            cc_addresses=cc_addrs,
            date=date,
            flags=flags,
            size=size,
        )

    def fetch_emails(
        self,
        mailbox: Optional[str] = None,
        limit: int = 20,
        offset: int = 0,
        since: Optional[str] = None,
        before: Optional[str] = None,
    ) -> list[EmailHeader]:
        """Fetch emails from mailbox with optional filters."""
        self._ensure_connected()
        if mailbox:
            self.select_mailbox(mailbox)
        elif not self.current_mailbox:
            self.select_mailbox("INBOX")

        # Build search criteria
        criteria = ["ALL"]
        if since:
            criteria = ["SINCE", self._to_imap_date(since)]
        if before:
            if len(criteria) > 1:
                criteria.extend(["BEFORE", self._to_imap_date(before)])
            else:
                criteria = ["BEFORE", self._to_imap_date(before)]

        uids = self.client.search(criteria)

        # Apply offset and limit (newest first)
        uids = sorted(uids, reverse=True)
        if offset:
            uids = uids[offset:]
        if limit:
            uids = uids[:limit]

        if not uids:
            return []

        # Serve from cache where possible, fetch rest from IMAP
        effective_mailbox = mailbox or self.current_mailbox or "INBOX"
        if self.email_cache:
            results = []
            uncached_uids = []
            for uid in uids:
                cached = self.email_cache.get_email(effective_mailbox, uid)
                if cached:
                    results.append(self._cached_to_header(cached))
                else:
                    uncached_uids.append(uid)
            if uncached_uids:
                messages = self.client.fetch(uncached_uids, ["ENVELOPE", "FLAGS", "RFC822.SIZE"])
                for uid, data in messages.items():
                    hdr = self._parse_email_header(uid, data)
                    self.email_cache.store_email(
                        effective_mailbox, uid, self._header_to_cache_dict(hdr)
                    )
                    results.append(hdr)
            return sorted(results, key=lambda h: h.date or datetime.min, reverse=True)

        messages = self.client.fetch(uids, ["ENVELOPE", "FLAGS", "RFC822.SIZE"])
        return [self._parse_email_header(uid, data) for uid, data in messages.items()]

    def get_email(self, uid: int, mailbox: Optional[str] = None) -> Email:
        """Get complete email by UID."""
        self._ensure_connected()
        if mailbox:
            self.select_mailbox(mailbox)

        effective_mailbox = mailbox or self.current_mailbox or "INBOX"

        # Check cache first
        if self.email_cache:
            cached = self.email_cache.get_email(effective_mailbox, uid)
            if cached and cached.get("has_body"):
                return self._cached_to_email(cached)

        data = self.client.fetch([uid], ["ENVELOPE", "FLAGS", "RFC822.SIZE", "BODY[]"])
        if uid not in data:
            raise ValueError(f"Email with UID {uid} not found")

        msg_data = data[uid]
        header = self._parse_email_header(uid, msg_data)

        # Parse body
        raw_body = msg_data.get(b"BODY[]", b"")
        msg = email.message_from_bytes(raw_body)
        body = self._extract_body(msg)
        attachments = self._extract_attachment_info(msg)

        # Store in cache opportunistically
        if self.email_cache:
            self.email_cache.store_email(
                effective_mailbox, uid,
                self._header_to_cache_dict(header),
                {"text": body.text, "html": body.html},
            )
            for att in attachments:
                att_data = self._get_attachment_bytes(msg, att.index)
                if att_data:
                    self.email_cache.store_attachment(
                        effective_mailbox, uid, att.index,
                        att.filename, att.content_type, att.size, att_data,
                    )

        return Email(header=header, body=body, attachments=attachments)

    def get_email_headers(self, uid: int, mailbox: Optional[str] = None) -> EmailHeader:
        """Get only email headers (faster)."""
        self._ensure_connected()
        if mailbox:
            self.select_mailbox(mailbox)

        data = self.client.fetch([uid], ["ENVELOPE", "FLAGS", "RFC822.SIZE"])
        if uid not in data:
            raise ValueError(f"Email with UID {uid} not found")

        return self._parse_email_header(uid, data[uid])

    def get_email_body(
        self, uid: int, mailbox: Optional[str] = None, format: str = "text"  # noqa: A002
    ) -> str:
        """Get email body content.

        Args:
            uid: Email UID.
            mailbox: Mailbox name (defaults to current).
            format: ``"text"`` or ``"html"``.
        """
        self._ensure_connected()
        if mailbox:
            self.select_mailbox(mailbox)

        effective_mailbox = mailbox or self.current_mailbox or "INBOX"

        # Check cache first
        if self.email_cache:
            cached = self.email_cache.get_email(effective_mailbox, uid)
            if cached and cached.get("has_body"):
                if format == "html" and cached.get("body_html"):
                    return cached["body_html"]
                return cached.get("body_text") or ""

        data = self.client.fetch([uid], ["BODY[]"])
        if uid not in data:
            raise ValueError(f"Email with UID {uid} not found")

        raw_body = data[uid].get(b"BODY[]", b"")
        msg = email.message_from_bytes(raw_body)
        body = self._extract_body(msg)

        if format == "html" and body.html:
            return body.html
        return body.text or ""

    def _extract_body(self, msg: email.message.Message) -> EmailBody:
        """Extract text and HTML body from email message."""
        text_body = None
        html_body = None

        if msg.is_multipart():
            for part in msg.walk():
                content_type = part.get_content_type()
                content_disposition = str(part.get("Content-Disposition", ""))

                if "attachment" in content_disposition:
                    continue

                if content_type == "text/plain" and not text_body:
                    payload = part.get_payload(decode=True)
                    charset = part.get_content_charset() or "utf-8"
                    text_body = payload.decode(charset, errors="replace")
                elif content_type == "text/html" and not html_body:
                    payload = part.get_payload(decode=True)
                    charset = part.get_content_charset() or "utf-8"
                    html_body = payload.decode(charset, errors="replace")
        else:
            content_type = msg.get_content_type()
            payload = msg.get_payload(decode=True)
            charset = msg.get_content_charset() or "utf-8"
            if payload:
                decoded = payload.decode(charset, errors="replace")
                if content_type == "text/html":
                    html_body = decoded
                else:
                    text_body = decoded

        # HTML-only fallback: convert HTML to readable plain text so the cache,
        # FTS index and snippet generators have searchable content.
        if not text_body and html_body:
            text_body = html_to_plain(html_body)

        return EmailBody(text=text_body, html=html_body)

    def _extract_attachment_info(self, msg: email.message.Message) -> list[Attachment]:
        """Extract attachment metadata from email."""
        attachments = []
        index = 0

        for part in msg.walk():
            content_disposition = str(part.get("Content-Disposition", ""))
            if "attachment" in content_disposition or "inline" in content_disposition:
                filename = part.get_filename()
                if filename:
                    filename = self._decode_header(filename)
                    content_type = part.get_content_type()
                    payload = part.get_payload(decode=True)
                    size = len(payload) if payload else None

                    attachments.append(Attachment(
                        index=index,
                        filename=filename,
                        content_type=content_type,
                        size=size,
                    ))
                    index += 1

        return attachments

    def get_attachments(self, uid: int, mailbox: Optional[str] = None) -> list[Attachment]:
        """List attachments of an email."""
        self._ensure_connected()
        if mailbox:
            self.select_mailbox(mailbox)

        effective_mailbox = mailbox or self.current_mailbox or "INBOX"

        # Check cache first
        if self.email_cache:
            cached_atts = self.email_cache.get_attachments(effective_mailbox, uid)
            if cached_atts:
                return [
                    Attachment(
                        index=a["idx"], filename=a["filename"],
                        content_type=a["content_type"], size=a.get("size"),
                    )
                    for a in cached_atts
                ]

        data = self.client.fetch([uid], ["BODY[]"])
        if uid not in data:
            raise ValueError(f"Email with UID {uid} not found")

        raw_body = data[uid].get(b"BODY[]", b"")
        msg = email.message_from_bytes(raw_body)
        return self._extract_attachment_info(msg)

    def download_attachment(
        self, uid: int, attachment_index: int, mailbox: Optional[str] = None
    ) -> tuple[str, str, bytes]:
        """Download attachment content (returns filename, content_type, base64 data)."""
        self._ensure_connected()
        if mailbox:
            self.select_mailbox(mailbox)

        effective_mailbox = mailbox or self.current_mailbox or "INBOX"

        # Check cache first
        if self.email_cache:
            result = self.email_cache.get_attachment_data(
                effective_mailbox, uid, attachment_index
            )
            if result:
                return result[0], result[1], base64.b64encode(result[2])

        data = self.client.fetch([uid], ["BODY[]"])
        if uid not in data:
            raise ValueError(f"Email with UID {uid} not found")

        raw_body = data[uid].get(b"BODY[]", b"")
        msg = email.message_from_bytes(raw_body)

        index = 0
        for part in msg.walk():
            content_disposition = str(part.get("Content-Disposition", ""))
            if "attachment" in content_disposition or "inline" in content_disposition:
                filename = part.get_filename()
                if filename:
                    if index == attachment_index:
                        filename = self._decode_header(filename)
                        content_type = part.get_content_type()
                        payload = part.get_payload(decode=True)
                        return filename, content_type, base64.b64encode(payload)
                    index += 1

        raise ValueError(f"Attachment at index {attachment_index} not found")

    def get_thread(self, uid: int, mailbox: Optional[str] = None) -> list[EmailHeader]:
        """Get the email thread/conversation that contains ``uid``.

        Resolution order, falling back gracefully:

        1. **IMAP THREAD REFERENCES** -- if the server advertises THREAD
           support, ask it to compute the thread and return every UID in
           the same thread group.
        2. **Local Message-ID / References** -- if the persistent cache has
           the email's References/In-Reply-To chain, walk it locally.
        3. **Subject heuristic** -- the original behaviour (search by
           Re:-stripped subject).

        The result is always sorted by date, oldest first.
        """
        self._ensure_connected()
        if mailbox:
            self.select_mailbox(mailbox)

        effective_mailbox = mailbox or self.current_mailbox or "INBOX"

        # --- 1) Try IMAP THREAD REFERENCES ----------------------------------
        try:
            caps = {c.upper() for c in self._get_capabilities_set()}
            if "THREAD=REFERENCES" in caps or "THREAD=ORDEREDSUBJECT" in caps:
                algo = "REFERENCES" if "THREAD=REFERENCES" in caps else "ORDEREDSUBJECT"
                try:
                    threads = self.client.thread(algorithm=algo, criteria=["ALL"])
                except TypeError:
                    threads = self.client.thread(algorithm=algo)
                thread_uids = self._find_uid_in_threads(threads, uid)
                if thread_uids:
                    messages = self.client.fetch(
                        thread_uids, ["ENVELOPE", "FLAGS", "RFC822.SIZE"]
                    )
                    headers = [
                        self._parse_email_header(u, d) for u, d in messages.items()
                    ]
                    return sorted(headers, key=lambda h: h.date or datetime.min)
        except Exception as exc:
            logger_imap.debug("THREAD REFERENCES unavailable: %s", exc)

        # --- 2) Local Message-ID / References ------------------------------
        if self.email_cache:
            related_uids = self._thread_via_local_references(effective_mailbox, uid)
            if len(related_uids) > 1:
                messages = self.client.fetch(
                    sorted(related_uids), ["ENVELOPE", "FLAGS", "RFC822.SIZE"]
                )
                headers = [
                    self._parse_email_header(u, d) for u, d in messages.items()
                ]
                return sorted(headers, key=lambda h: h.date or datetime.min)

        # --- 3) Subject heuristic (legacy fallback) -----------------------
        email_data = self.get_email(uid, mailbox)
        subject = email_data.header.subject
        if subject:
            clean_subject = subject
            for prefix in ["Re:", "RE:", "Fwd:", "FWD:", "Fw:", "AW:", "Aw:"]:
                clean_subject = clean_subject.replace(prefix, "").strip()
            uids = self.client.search(["SUBJECT", clean_subject], charset="UTF-8")
            if uids:
                messages = self.client.fetch(uids, ["ENVELOPE", "FLAGS", "RFC822.SIZE"])
                headers = [
                    self._parse_email_header(u, d) for u, d in messages.items()
                ]
                return sorted(headers, key=lambda h: h.date or datetime.min)

        return [email_data.header]

    @staticmethod
    def _find_uid_in_threads(threads, uid: int) -> list[int]:
        """Return every UID belonging to the same top-level thread as ``uid``.

        IMAPClient returns nested tuples of ints; flatten the group containing
        the requested uid.
        """
        def _flatten(node, acc):
            if isinstance(node, (list, tuple)):
                for x in node:
                    _flatten(x, acc)
            else:
                acc.append(int(node))
            return acc

        for group in threads or []:
            flat = _flatten(group, [])
            if uid in flat:
                return flat
        return []

    def _thread_via_local_references(
        self, mailbox: str, uid: int
    ) -> set[int]:
        """Walk the cached Message-ID/References graph to assemble a thread."""
        if not self.email_cache:
            return {uid}

        # Need the full BODY[HEADER.FIELDS (Message-ID In-Reply-To References)]
        # but for cached emails we just have message_id. Re-fetch headers for
        # the seed.
        try:
            data = self.client.fetch(
                [uid],
                ["BODY.PEEK[HEADER.FIELDS (Message-ID In-Reply-To References)]"],
            )
        except Exception:
            return {uid}

        msg_data = data.get(uid, {})
        raw_header = msg_data.get(
            b"BODY[HEADER.FIELDS (Message-ID In-Reply-To References)]", b""
        )
        if not raw_header:
            raw_header = msg_data.get(
                b"BODY[HEADER.FIELDS (MESSAGE-ID IN-REPLY-TO REFERENCES)]", b""
            )
        if not raw_header:
            return {uid}

        seed = email.message_from_bytes(raw_header)
        ids: set[str] = set()
        for h in ("Message-ID", "In-Reply-To"):
            v = seed.get(h)
            if v:
                ids.add(v.strip())
        for v in (seed.get("References", "") or "").split():
            ids.add(v.strip())

        if not ids:
            return {uid}

        placeholders = ", ".join("?" * len(ids))
        rows = self.email_cache.conn.execute(
            f"SELECT uid FROM emails WHERE mailbox = ? AND message_id IN ({placeholders})",
            (mailbox, *ids),
        ).fetchall()
        return {uid, *(r["uid"] for r in rows)}

    def _get_capabilities_set(self) -> set[str]:
        """Return the IMAP server's CAPABILITY set as a set of strings."""
        if not self.client:
            return set()
        caps = self.client.capabilities() or ()
        return {c.decode() if isinstance(c, bytes) else str(c) for c in caps}

    # === Server metadata =================================================

    def get_capabilities(self) -> list[str]:
        """Return the IMAP server's advertised capabilities."""
        self._ensure_connected()
        return sorted(self._get_capabilities_set())

    def get_namespace(self) -> dict:
        """Return the IMAP NAMESPACE result (personal/other/shared)."""
        self._ensure_connected()
        try:
            result = self.client.namespace()
        except Exception as exc:
            return {"error": str(exc)}

        def _norm(items):
            if not items:
                return []
            out = []
            for prefix, delim in items:
                p = prefix.decode() if isinstance(prefix, bytes) else prefix
                d = delim.decode() if isinstance(delim, bytes) else delim
                out.append({"prefix": p, "delimiter": d})
            return out

        return {
            "personal": _norm(getattr(result, "personal", None)),
            "other_users": _norm(getattr(result, "other_users", None)),
            "shared": _norm(getattr(result, "shared", None)),
        }

    def get_quota(self, mailbox: Optional[str] = None) -> dict:
        """Return IMAP QUOTA usage for a mailbox (default: INBOX)."""
        self._ensure_connected()
        target = mailbox or "INBOX"
        try:
            quotas = self.client.get_quota_root(target)
        except Exception as exc:
            return {"error": str(exc), "mailbox": target}

        # IMAPClient returns (quota_roots, quota_resources)
        if isinstance(quotas, tuple) and len(quotas) == 2:
            roots, resources = quotas
        else:
            roots, resources = [], quotas or []

        roots_out = []
        for r in roots or []:
            if hasattr(r, "mailbox"):
                roots_out.append({
                    "mailbox": (r.mailbox.decode() if isinstance(r.mailbox, bytes) else r.mailbox),
                    "quota_root": (r.quota_root.decode() if isinstance(r.quota_root, bytes) else r.quota_root),
                })
            else:
                roots_out.append({"mailbox": str(r)})

        resources_out = []
        for q in resources or []:
            if hasattr(q, "resource"):
                resources_out.append({
                    "quota_root": (q.quota_root.decode() if isinstance(q.quota_root, bytes) else q.quota_root),
                    "resource": (q.resource.decode() if isinstance(q.resource, bytes) else q.resource),
                    "usage": int(q.usage),
                    "limit": int(q.limit),
                })
            else:
                resources_out.append({"raw": str(q)})

        return {"mailbox": target, "quota_roots": roots_out, "resources": resources_out}

    def get_server_id(self) -> dict:
        """Return IMAP ID server info (RFC 2971), or {} if unsupported."""
        self._ensure_connected()
        try:
            result = self.client.id_(
                {"name": "imap-mcp", "version": "1.0.0", "vendor": "newlc"}
            )
        except Exception as exc:
            return {"error": str(exc)}
        if not result:
            return {}
        out = {}
        for k, v in (result.items() if hasattr(result, "items") else []):
            key = k.decode() if isinstance(k, bytes) else str(k)
            val = v.decode() if isinstance(v, bytes) else (None if v is None else str(v))
            out[key] = val
        return out

    # === Cache helpers ===

    def _header_to_cache_dict(self, header: EmailHeader) -> dict:
        """Convert EmailHeader model to a dict suitable for cache storage."""
        return {
            "message_id": header.message_id,
            "subject": header.subject,
            "from_address": header.from_address,
            "to_addresses": header.to_addresses,
            "cc_addresses": header.cc_addresses,
            "date": header.date,
            "flags": header.flags,
            "size": header.size,
        }

    def _cached_to_header(self, cached: dict) -> EmailHeader:
        """Convert a cached email dict back to an EmailHeader model."""
        from_addr = None
        if cached.get("from_email"):
            from_addr = EmailAddress(
                name=cached.get("from_name"),
                email=cached["from_email"],
            )
        to_addrs = []
        if cached.get("to_json"):
            for a in json.loads(cached["to_json"]):
                to_addrs.append(EmailAddress(**a))
        cc_addrs = []
        if cached.get("cc_json"):
            for a in json.loads(cached["cc_json"]):
                cc_addrs.append(EmailAddress(**a))
        flags = []
        if cached.get("flags"):
            flags = json.loads(cached["flags"])
        date = None
        if cached.get("date"):
            try:
                date = datetime.fromisoformat(cached["date"])
            except (ValueError, TypeError):
                pass
        return EmailHeader(
            uid=cached["uid"],
            message_id=cached.get("message_id"),
            subject=cached.get("subject"),
            from_address=from_addr,
            to_addresses=to_addrs,
            cc_addresses=cc_addrs,
            date=date,
            flags=flags,
            size=cached.get("size"),
        )

    def _cached_to_email(self, cached: dict) -> Email:
        """Convert a cached email dict back to full Email model."""
        header = self._cached_to_header(cached)
        body = None
        if cached.get("has_body"):
            body = EmailBody(
                text=cached.get("body_text"),
                html=cached.get("body_html"),
            )
        atts = []
        if self.email_cache:
            for a in self.email_cache.get_attachments(cached["mailbox"], cached["uid"]):
                atts.append(Attachment(
                    index=a["idx"],
                    filename=a["filename"],
                    content_type=a["content_type"],
                    size=a.get("size"),
                ))
        return Email(header=header, body=body, attachments=atts)

    def _get_attachment_bytes(
        self, msg: email.message.Message, attachment_index: int
    ) -> Optional[bytes]:
        """Extract raw bytes for a given attachment index."""
        index = 0
        for part in msg.walk():
            content_disposition = str(part.get("Content-Disposition", ""))
            if "attachment" in content_disposition or "inline" in content_disposition:
                filename = part.get_filename()
                if filename:
                    if index == attachment_index:
                        return part.get_payload(decode=True)
                    index += 1
        return None

    # === Sync ===

    def sync_emails(
        self,
        mailbox: str = "INBOX",
        since: Optional[str] = None,
        before: Optional[str] = None,
        full: bool = False,
    ) -> dict:
        """Download emails into persistent SQLite cache.

        Incremental by default — only fetches UIDs not already cached.
        Set full=True to re-download everything in the date range.
        """
        self._ensure_connected()
        if not self.email_cache:
            raise RuntimeError("Cache not initialized. Call auto_connect() first.")

        self.select_mailbox(mailbox)

        # Check UIDVALIDITY
        status = self.get_mailbox_status(mailbox)
        self.email_cache.check_uidvalidity(mailbox, status.uidvalidity)

        # Build IMAP search criteria
        criteria = []
        if since:
            criteria.extend(["SINCE", self._to_imap_date(since)])
        if before:
            criteria.extend(["BEFORE", self._to_imap_date(before)])
        if not criteria:
            criteria = ["ALL"]

        uids = self.client.search(criteria)

        # Incremental: skip already-cached UIDs (with body)
        cached_uids = set()
        if not full:
            cached_uids = self.email_cache.get_cached_uids_with_body(mailbox)
            uids = [u for u in uids if u not in cached_uids]

        total_in_range = len(uids) + len(cached_uids)

        if not uids:
            return {
                "synced": 0,
                "total_in_range": total_in_range,
                "already_cached": len(cached_uids),
                "mailbox": mailbox,
                "message": "Already up to date",
            }

        # Fetch in batches of 50
        synced = 0
        errors = 0
        for i in range(0, len(uids), 50):
            batch = uids[i:i + 50]
            try:
                messages = self.client.fetch(
                    batch, ["ENVELOPE", "FLAGS", "RFC822.SIZE", "BODY[]"]
                )
            except Exception:
                errors += len(batch)
                continue

            for uid, data in messages.items():
                try:
                    header = self._parse_email_header(uid, data)
                    raw_body = data.get(b"BODY[]", b"")
                    msg = email.message_from_bytes(raw_body)
                    body = self._extract_body(msg)
                    attachments_info = self._extract_attachment_info(msg)

                    self.email_cache.store_email(
                        mailbox, uid,
                        self._header_to_cache_dict(header),
                        {"text": body.text, "html": body.html},
                    )

                    for att in attachments_info:
                        att_data = self._get_attachment_bytes(msg, att.index)
                        if att_data:
                            self.email_cache.store_attachment(
                                mailbox, uid, att.index,
                                att.filename, att.content_type,
                                att.size, att_data,
                            )
                    synced += 1
                except Exception:
                    errors += 1

        self.email_cache.update_last_sync(mailbox, status.uidvalidity)
        self.email_cache.flush()

        result = {
            "synced": synced,
            "total_in_range": total_in_range,
            "already_cached": len(cached_uids),
            "errors": errors,
            "mailbox": mailbox,
        }
        if errors:
            result["message"] = f"Synced {synced}, {errors} errors"
        else:
            result["message"] = f"Synced {synced} emails"
        return result

    def get_cache_stats(self) -> dict:
        """Return cache statistics."""
        if not self.email_cache:
            return {"error": "Cache not initialized"}
        return self.email_cache.stats()

    def cleanup_sent_log(self, older_than_days: int = 30) -> dict:
        """Remove sent_log rows older than ``older_than_days`` days."""
        if not self.email_cache:
            raise RuntimeError("Persistent cache is disabled.")
        return self.email_cache.cleanup_sent_log(older_than_days=older_than_days)

    def vacuum_cache(self) -> dict:
        """Compact the cache database (VACUUM + FTS5 optimize)."""
        if not self.email_cache:
            raise RuntimeError("Persistent cache is disabled.")
        return self.email_cache.vacuum()

    def export_cache(self, passphrase: str, output_path: str) -> dict:
        """Write a passphrase-protected, machine-portable cache snapshot."""
        if not self.email_cache:
            raise RuntimeError("Persistent cache is disabled.")
        return self.email_cache.export_portable(passphrase, output_path)

    def import_cache(self, passphrase: str, input_path: str) -> dict:
        """Replace the live cache with the contents of a portable export."""
        if not self.email_cache:
            raise RuntimeError("Persistent cache is disabled.")
        return self.email_cache.import_portable(passphrase, input_path)

    def rotate_encryption_key(self) -> dict:
        """Re-encrypt the on-disk cache with a fresh Fernet key.

        Backs up the previous key under ``<keyring_username>.previous`` in
        the OS keyring so a botched rotation is recoverable.
        """
        if not self.email_cache:
            raise RuntimeError("Persistent cache is disabled.")
        return self.email_cache.rotate_encryption_key()

    def load_cache(
        self,
        mailbox: str = "INBOX",
        mode: str = "recent",
        count: int = 100,
        since: Optional[str] = None,
        before: Optional[str] = None,
        include_attachments: bool = True,
    ) -> dict:
        """Flexible cache loader.

        Modes:
          recent  — load the *count* most recent emails
          new     — load only emails newer than what's already cached
          older   — load *count* emails older than the oldest cached email
          range   — load emails between *since* and *before* dates
        """
        self._ensure_connected()
        if not self.email_cache:
            raise RuntimeError("Cache not initialized. Call auto_connect() first.")

        self.select_mailbox(mailbox)
        status = self.get_mailbox_status(mailbox)
        self.email_cache.check_uidvalidity(mailbox, status.uidvalidity)

        cached_uids = self.email_cache.get_cached_uids_with_body(mailbox)

        if mode == "recent":
            # Get all UIDs, take the newest *count*
            all_uids = self.client.search(["ALL"])
            all_uids = sorted(all_uids, reverse=True)
            target_uids = all_uids[:count]
            to_fetch = [u for u in target_uids if u not in cached_uids]

        elif mode == "new":
            # Find UIDs newer than the max cached UID
            max_uid = self.email_cache.get_max_uid(mailbox)
            if max_uid is not None:
                # IMAP UID search: UIDs > max_uid
                all_uids = self.client.search(["UID", f"{max_uid + 1}:*"])
                # Filter out max_uid itself (IMAP range is inclusive)
                to_fetch = [u for u in all_uids if u > max_uid and u not in cached_uids]
            else:
                # Nothing cached yet — fall back to recent
                all_uids = self.client.search(["ALL"])
                all_uids = sorted(all_uids, reverse=True)
                to_fetch = all_uids[:count]

        elif mode == "older":
            # Find UIDs older than the min cached UID
            min_uid = self.email_cache.get_min_uid(mailbox)
            if min_uid is not None and min_uid > 1:
                all_uids = self.client.search(["UID", f"1:{min_uid - 1}"])
                all_uids = sorted(all_uids, reverse=True)
                to_fetch = [u for u in all_uids[:count] if u not in cached_uids]
            else:
                to_fetch = []

        elif mode == "range":
            criteria = []
            if since:
                criteria.extend(["SINCE", self._to_imap_date(since)])
            if before:
                criteria.extend(["BEFORE", self._to_imap_date(before)])
            if not criteria:
                criteria = ["ALL"]
            all_uids = self.client.search(criteria)
            to_fetch = [u for u in all_uids if u not in cached_uids]

        else:
            return {"error": f"Unknown mode: {mode}. Use: recent, new, older, range"}

        if not to_fetch:
            return {
                "loaded": 0,
                "already_cached": len(cached_uids),
                "cached_total": self.email_cache.get_cached_count(mailbox),
                "mailbox": mailbox,
                "mode": mode,
                "message": "Nothing new to load",
            }

        # Fetch in batches
        loaded = 0
        errors = 0
        fetch_fields = ["ENVELOPE", "FLAGS", "RFC822.SIZE", "BODY[]"]

        for i in range(0, len(to_fetch), 50):
            batch = to_fetch[i:i + 50]
            try:
                messages = self.client.fetch(batch, fetch_fields)
            except Exception:
                errors += len(batch)
                continue

            for uid, data in messages.items():
                try:
                    header = self._parse_email_header(uid, data)
                    raw_body = data.get(b"BODY[]", b"")
                    msg = email.message_from_bytes(raw_body)
                    body = self._extract_body(msg)

                    self.email_cache.store_email(
                        mailbox, uid,
                        self._header_to_cache_dict(header),
                        {"text": body.text, "html": body.html},
                    )

                    if include_attachments:
                        attachments_info = self._extract_attachment_info(msg)
                        for att in attachments_info:
                            att_data = self._get_attachment_bytes(msg, att.index)
                            if att_data:
                                self.email_cache.store_attachment(
                                    mailbox, uid, att.index,
                                    att.filename, att.content_type,
                                    att.size, att_data,
                                )
                    loaded += 1
                except Exception:
                    errors += 1

        self.email_cache.update_last_sync(mailbox, status.uidvalidity)
        self.email_cache.flush()

        return {
            "loaded": loaded,
            "errors": errors,
            "already_cached": len(cached_uids),
            "cached_total": self.email_cache.get_cached_count(mailbox),
            "mailbox": mailbox,
            "mode": mode,
            "message": f"Loaded {loaded} emails" + (f", {errors} errors" if errors else ""),
        }

    # === Search Operations ===

    def search_emails(
        self, query: str, mailbox: Optional[str] = None, limit: int = 50
    ) -> list[EmailHeader]:
        """Search emails with query (IMAP SEARCH syntax or text)."""
        self._ensure_connected()
        if mailbox:
            self.select_mailbox(mailbox)
        elif not self.current_mailbox:
            self.select_mailbox("INBOX")

        # Try to parse as IMAP search criteria, fallback to TEXT search
        try:
            if any(kw in query.upper() for kw in ["FROM", "TO", "SUBJECT", "BODY", "ALL", "UNSEEN"]):
                uids = self.client.search(query.split())
            else:
                uids = self.client.search(["TEXT", query])
        except Exception:
            uids = self.client.search(["TEXT", query])

        uids = sorted(uids, reverse=True)[:limit]
        if not uids:
            return []

        messages = self.client.fetch(uids, ["ENVELOPE", "FLAGS", "RFC822.SIZE"])
        return [self._parse_email_header(uid, data) for uid, data in messages.items()]

    def search_by_sender(
        self, sender: str, mailbox: Optional[str] = None, limit: int = 50
    ) -> list[EmailHeader]:
        """Search emails by sender address."""
        self._ensure_connected()
        if mailbox:
            self.select_mailbox(mailbox)
        elif not self.current_mailbox:
            self.select_mailbox("INBOX")

        uids = self.client.search(["FROM", sender])
        uids = sorted(uids, reverse=True)[:limit]
        if not uids:
            return []

        messages = self.client.fetch(uids, ["ENVELOPE", "FLAGS", "RFC822.SIZE"])
        return [self._parse_email_header(uid, data) for uid, data in messages.items()]

    def search_by_subject(
        self, subject: str, mailbox: Optional[str] = None, limit: int = 50
    ) -> list[EmailHeader]:
        """Search emails by subject."""
        self._ensure_connected()
        if mailbox:
            self.select_mailbox(mailbox)
        elif not self.current_mailbox:
            self.select_mailbox("INBOX")

        uids = self.client.search(["SUBJECT", subject])
        uids = sorted(uids, reverse=True)[:limit]
        if not uids:
            return []

        messages = self.client.fetch(uids, ["ENVELOPE", "FLAGS", "RFC822.SIZE"])
        return [self._parse_email_header(uid, data) for uid, data in messages.items()]

    def search_by_date(
        self,
        mailbox: Optional[str] = None,
        since: Optional[str] = None,
        before: Optional[str] = None,
        limit: int = 50,
    ) -> list[EmailHeader]:
        """Search emails by date range."""
        self._ensure_connected()
        if mailbox:
            self.select_mailbox(mailbox)
        elif not self.current_mailbox:
            self.select_mailbox("INBOX")

        criteria = []
        if since:
            criteria.extend(["SINCE", self._to_imap_date(since)])
        if before:
            criteria.extend(["BEFORE", self._to_imap_date(before)])
        if not criteria:
            criteria = ["ALL"]

        uids = self.client.search(criteria)
        uids = sorted(uids, reverse=True)[:limit]
        if not uids:
            return []

        messages = self.client.fetch(uids, ["ENVELOPE", "FLAGS", "RFC822.SIZE"])
        return [self._parse_email_header(uid, data) for uid, data in messages.items()]

    def search_unread(
        self, mailbox: Optional[str] = None, limit: int = 50
    ) -> list[EmailHeader]:
        """Get all unread emails."""
        self._ensure_connected()
        if mailbox:
            self.select_mailbox(mailbox)
        elif not self.current_mailbox:
            self.select_mailbox("INBOX")

        uids = self.client.search(["UNSEEN"])
        uids = sorted(uids, reverse=True)[:limit]
        if not uids:
            return []

        messages = self.client.fetch(uids, ["ENVELOPE", "FLAGS", "RFC822.SIZE"])
        return [self._parse_email_header(uid, data) for uid, data in messages.items()]

    def search_flagged(
        self, mailbox: Optional[str] = None, limit: int = 50
    ) -> list[EmailHeader]:
        """Get all flagged/starred emails."""
        self._ensure_connected()
        if mailbox:
            self.select_mailbox(mailbox)
        elif not self.current_mailbox:
            self.select_mailbox("INBOX")

        uids = self.client.search(["FLAGGED"])
        uids = sorted(uids, reverse=True)[:limit]
        if not uids:
            return []

        messages = self.client.fetch(uids, ["ENVELOPE", "FLAGS", "RFC822.SIZE"])
        return [self._parse_email_header(uid, data) for uid, data in messages.items()]

    # === Email Actions ===

    def mark_read(self, uids: list[int], mailbox: Optional[str] = None) -> bool:
        """Mark emails as read."""
        self._ensure_connected()
        if mailbox:
            self.select_mailbox(mailbox)
        self.client.add_flags(uids, [b"\\Seen"])
        return True

    def mark_unread(self, uids: list[int], mailbox: Optional[str] = None) -> bool:
        """Mark emails as unread."""
        self._ensure_connected()
        if mailbox:
            self.select_mailbox(mailbox)
        self.client.remove_flags(uids, [b"\\Seen"])
        return True

    def flag_email(
        self, uids: list[int], flag: str, mailbox: Optional[str] = None
    ) -> bool:
        """Add flag to emails."""
        self._ensure_connected()
        if mailbox:
            self.select_mailbox(mailbox)
        self.client.add_flags(uids, [flag.encode() if isinstance(flag, str) else flag])
        return True

    def unflag_email(
        self, uids: list[int], flag: str, mailbox: Optional[str] = None
    ) -> bool:
        """Remove flag from emails."""
        self._ensure_connected()
        if mailbox:
            self.select_mailbox(mailbox)
        self.client.remove_flags(uids, [flag.encode() if isinstance(flag, str) else flag])
        return True

    def move_email(
        self, uids: list[int], destination: str, mailbox: Optional[str] = None
    ) -> bool:
        """Move emails to another mailbox."""
        self._ensure_connected()
        if mailbox:
            self.select_mailbox(mailbox)
        try:
            self.client.move(uids, destination)
        except Exception as exc:
            if "namespace" in str(exc).lower() and not destination.startswith("INBOX."):
                self.client.move(uids, f"INBOX.{destination}")
            else:
                raise
        return True

    def copy_email(
        self, uids: list[int], destination: str, mailbox: Optional[str] = None
    ) -> bool:
        """Copy emails to another mailbox."""
        self._ensure_connected()
        if mailbox:
            self.select_mailbox(mailbox)
        try:
            self.client.copy(uids, destination)
        except Exception as exc:
            if "namespace" in str(exc).lower() and not destination.startswith("INBOX."):
                self.client.copy(uids, f"INBOX.{destination}")
            else:
                raise
        return True

    def archive_email(
        self,
        uids: list[int],
        mailbox: Optional[str] = None,
        archive_folder: str = "Archive",
    ) -> bool:
        """Archive emails (move to Archive folder)."""
        return self.move_email(uids, archive_folder, mailbox)

    def get_signature(self, fmt: str = "text") -> Optional[str]:
        """Get user signature from config.

        Args:
            fmt: ``"text"`` or ``"html"``.
        """
        user_config = self.config.get("user", {})
        sig_config = user_config.get("signature", {})

        if not sig_config.get("enabled", False):
            return None

        if fmt == "html":
            return sig_config.get("html")
        return sig_config.get("text")

    def _build_message(
        self,
        to: list[str],
        subject: str,
        body: str,
        cc: Optional[list[str]] = None,
        bcc: Optional[list[str]] = None,
        html_body: Optional[str] = None,
        attachments: Optional[list[str]] = None,
        include_signature: bool = True,
        include_bcc_header: bool = False,
        in_reply_to: Optional[str] = None,
        references: Optional[list[str]] = None,
    ) -> email.message.Message:
        """Build a MIME message from the given fields.

        ``include_bcc_header`` should be ``False`` when sending via SMTP
        (Bcc must not appear in the transmitted message) and ``True`` for
        drafts saved into IMAP, where the user expects to see the Bcc list
        when reviewing the draft.
        """
        final_body = body
        final_html = html_body
        if include_signature:
            text_sig = self.get_signature("text")
            if text_sig:
                final_body = body + text_sig
            if html_body:
                html_sig = self.get_signature("html")
                if html_sig:
                    final_html = html_body + html_sig

        if final_html:
            body_part = MIMEMultipart("alternative")
            body_part.attach(MIMEText(final_body, "plain", "utf-8"))
            body_part.attach(MIMEText(final_html, "html", "utf-8"))
        else:
            body_part = MIMEText(final_body, "plain", "utf-8")

        if attachments:
            self._validate_attachment_paths(attachments)
            msg = MIMEMultipart("mixed")
            msg.attach(body_part)
            for path_str in attachments:
                path = Path(path_str).expanduser()
                if not path.is_file():
                    raise FileNotFoundError(f"Attachment not found: {path}")
                ctype, encoding = mimetypes.guess_type(str(path))
                if ctype is None or encoding is not None:
                    ctype = "application/octet-stream"
                maintype, subtype = ctype.split("/", 1)
                with open(path, "rb") as fp:
                    part = MIMEBase(maintype, subtype)
                    part.set_payload(fp.read())
                encoders.encode_base64(part)
                part.add_header(
                    "Content-Disposition", "attachment", filename=path.name
                )
                msg.attach(part)
        else:
            msg = body_part

        user_config = self.config.get("user", {})
        from_name = user_config.get("name", "")
        from_email = user_config.get(
            "email", self.config.get("credentials", {}).get("username", "")
        )

        msg["To"] = ", ".join(to)
        msg["Subject"] = subject
        if from_name and from_email:
            msg["From"] = f"{from_name} <{from_email}>"
        elif from_email:
            msg["From"] = from_email
        if cc:
            msg["Cc"] = ", ".join(cc)
        if bcc and include_bcc_header:
            msg["Bcc"] = ", ".join(bcc)
        msg["Date"] = email.utils.formatdate(localtime=True)
        domain = from_email.split("@")[-1] if "@" in from_email else None
        msg["Message-ID"] = (
            email.utils.make_msgid(domain=domain) if domain else email.utils.make_msgid()
        )

        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
        if references:
            msg["References"] = " ".join(references)
        return msg

    def _validate_attachment_paths(self, attachments: list[str]) -> None:
        """Validate every attachment path against the security policy.

        Two checks:

        * **Size cap** -- ``security.max_attachment_size_mb`` (default 25 MB
          per file). Always enforced. Pass ``0`` to disable.
        * **Allowlist** (opt-in) -- ``security.attachments_allowed_dirs``
          (list of directories). When set, every resolved real path must
          live inside one of them, blocking prompt-injection from sneaking
          ``~/.ssh/id_rsa`` or ``/etc/passwd`` into an outgoing email.

        Symlinks are resolved before the allowlist check so symlinking out
        of an allowed directory is rejected.
        """
        sec = self.config.get("security", {}) or {}
        max_mb = sec.get("max_attachment_size_mb", 25)
        max_bytes = int(max_mb) * 1024 * 1024 if max_mb else 0

        allowed_dirs_raw = sec.get("attachments_allowed_dirs")
        allowed_dirs: list[Path] = []
        if allowed_dirs_raw:
            for d in allowed_dirs_raw:
                try:
                    allowed_dirs.append(Path(d).expanduser().resolve())
                except OSError:
                    pass

        for path_str in attachments:
            try:
                path = Path(path_str).expanduser()
                real = path.resolve(strict=False)
            except (OSError, RuntimeError) as exc:
                raise ValueError(f"Invalid attachment path {path_str!r}: {exc}")

            if not real.is_file():
                raise FileNotFoundError(f"Attachment not found: {real}")

            if max_bytes:
                size = real.stat().st_size
                if size > max_bytes:
                    raise ValueError(
                        f"Attachment {real.name!r} is {size / 1024 / 1024:.1f} MB; "
                        f"limit is {max_mb} MB. Increase "
                        f"security.max_attachment_size_mb or set 0 to disable."
                    )

            if allowed_dirs:
                if not any(_is_inside(real, d) for d in allowed_dirs):
                    raise PermissionError(
                        f"Attachment {real} is outside the configured "
                        f"security.attachments_allowed_dirs allowlist. "
                        f"Allowed: {[str(d) for d in allowed_dirs]}"
                    )

    def _safe_append(
        self, folder: str, msg_bytes: bytes, flags: list[bytes]
    ) -> str:
        """Append a message to ``folder``, retrying with ``INBOX.`` prefix
        on namespace-related errors. Returns the folder name actually used.
        """
        try:
            self.client.append(folder, msg_bytes, flags=flags)
            return folder
        except Exception as e:
            err = str(e).lower()
            if (
                ("namespace" in err or "no such" in err or "mailbox" in err)
                and not folder.startswith("INBOX.")
                and folder != "INBOX"
            ):
                prefixed = f"INBOX.{folder}"
                self.client.append(prefixed, msg_bytes, flags=flags)
                return prefixed
            raise

    def save_draft(
        self,
        to: list[str],
        subject: str,
        body: str,
        cc: Optional[list[str]] = None,
        bcc: Optional[list[str]] = None,
        html_body: Optional[str] = None,
        attachments: Optional[list[str]] = None,
        drafts_folder: str = "Drafts",
        include_signature: bool = True,
        idempotency_key: Optional[str] = None,
    ) -> dict:
        """Save email as draft (with optional file attachments).

        Returns ``{"saved": True, "drafts_folder": ..., "idempotent_replay": bool}``.

        With ``idempotency_key`` set and a persistent cache available, a
        second call with the same key returns the prior result without
        re-appending the draft -- avoids piling up duplicates if an agent
        retries after a network blip.
        """
        self._ensure_connected()

        # Reuse sent_log for draft idempotency: prefix the key so it never
        # collides with send_email's keyspace.
        key = f"draft:{idempotency_key}" if idempotency_key else None
        if key and self.email_cache:
            previous = self.email_cache.lookup_sent(key)
            if previous:
                return {
                    "saved": True,
                    "drafts_folder": previous.get("saved_to_sent"),
                    "idempotent_replay": True,
                }

        msg = self._build_message(
            to=to,
            subject=subject,
            body=body,
            cc=cc,
            bcc=bcc,
            html_body=html_body,
            attachments=attachments,
            include_signature=include_signature,
            include_bcc_header=True,
        )
        actual = self._safe_append(drafts_folder, msg.as_bytes(), [b"\\Draft"])

        if key and self.email_cache:
            try:
                self.email_cache.record_sent(
                    key, msg.get("Message-ID"),
                    list(to or []) + list(cc or []) + list(bcc or []),
                    subject, actual,
                )
            except Exception as exc:
                logger_imap.warning(
                    "save_draft succeeded but failed to record sent_log: %s", exc
                )

        return {"saved": True, "drafts_folder": actual, "idempotent_replay": False}

    # === Sending (write-mode) ===

    def _resolve_smtp_password(self) -> tuple[str, str]:
        """Resolve SMTP credentials (username, password) from config + keyring."""
        creds = self.config.get("credentials", {})
        username = creds.get("username", "")
        password = creds.get("password", "")
        if not password and username:
            password = get_stored_password(username)
        if not username or not password:
            raise RuntimeError(
                "No SMTP credentials available. "
                "Set credentials.username in config.json and run "
                "'imap-mcp --set-password'."
            )
        return username, password

    def _smtp_send(self, from_addr: str, recipients: list[str], msg_bytes: bytes) -> None:
        """Open an SMTP connection per ``self.config['smtp']`` and send."""
        smtp_config = self.config.get("smtp", {})
        smtp_host = smtp_config.get("host")
        if not smtp_host:
            raise RuntimeError(
                "SMTP not configured. Add 'smtp.host' (and 'smtp.port') to config.json."
            )
        smtp_port = smtp_config.get("port", 587)
        # SMTPS (implicit TLS, typically port 465) when secure=true.
        # STARTTLS (typically port 587) is the default for any other port.
        smtp_secure = smtp_config.get("secure", smtp_port == 465)
        smtp_starttls = smtp_config.get("starttls", not smtp_secure)

        username, password = self._resolve_smtp_password()

        def _send_once():
            if smtp_secure:
                ctx = ssl.create_default_context()
                conn = smtplib.SMTP_SSL(smtp_host, smtp_port, context=ctx)
            else:
                conn = smtplib.SMTP(smtp_host, smtp_port)
                if smtp_starttls:
                    conn.starttls(context=ssl.create_default_context())
            try:
                conn.login(username, password)
                conn.sendmail(from_addr, recipients, msg_bytes)
            finally:
                try:
                    conn.quit()
                except Exception:
                    pass

        # Retry on transient network errors only -- SMTP auth failures and
        # 5xx replies will surface on the first attempt and propagate.
        with_retries(_send_once)

    def send_email(
        self,
        to: list[str],
        subject: str,
        body: str,
        cc: Optional[list[str]] = None,
        bcc: Optional[list[str]] = None,
        html_body: Optional[str] = None,
        attachments: Optional[list[str]] = None,
        include_signature: bool = True,
        save_to_sent: bool = True,
        sent_folder: Optional[str] = None,
        in_reply_to: Optional[str] = None,
        references: Optional[list[str]] = None,
        idempotency_key: Optional[str] = None,
    ) -> dict:
        """Send an email via SMTP, optionally saving a copy to the Sent folder.

        Bcc recipients receive the message but are not listed in its headers.
        Returns a dict with ``sent``, ``message_id``, ``saved_to_sent``
        (folder name, or ``None`` if the IMAP append failed or was skipped),
        and ``idempotent_replay`` -- ``True`` when this exact key was already
        seen and the message was *not* re-sent.

        ``idempotency_key`` (when set together with a persistent cache)
        guards against duplicate sends if the agent retries a tool call
        because of a network blip after SMTP has already accepted the
        message. The first successful send writes ``(key, message_id,
        recipients, subject, saved_to_sent, sent_at)`` to the local
        ``sent_log`` table; subsequent calls with the same key return that
        record without contacting SMTP again.
        """
        self._ensure_connected()

        # ---- Idempotency: short-circuit if we've seen this key before ----
        if idempotency_key and self.email_cache:
            previous = self.email_cache.lookup_sent(idempotency_key)
            if previous:
                return {
                    "sent": True,
                    "message_id": previous.get("message_id"),
                    "saved_to_sent": previous.get("saved_to_sent"),
                    "idempotent_replay": True,
                    "sent_at": previous.get("sent_at"),
                }

        msg = self._build_message(
            to=to,
            subject=subject,
            body=body,
            cc=cc,
            bcc=bcc,
            html_body=html_body,
            attachments=attachments,
            include_signature=include_signature,
            include_bcc_header=False,
            in_reply_to=in_reply_to,
            references=references,
        )

        user_config = self.config.get("user", {})
        from_email = user_config.get(
            "email", self.config.get("credentials", {}).get("username", "")
        )
        envelope_recipients = list(to or [])
        if cc:
            envelope_recipients.extend(cc)
        if bcc:
            envelope_recipients.extend(bcc)

        msg_bytes = msg.as_bytes()
        self._smtp_send(from_email, envelope_recipients, msg_bytes)

        saved_to: Optional[str] = None
        if save_to_sent:
            target = sent_folder or self.config.get("folders", {}).get("sent", "Sent")
            try:
                saved_to = self._safe_append(target, msg_bytes, [b"\\Seen"])
            except Exception:
                # SMTP send already succeeded — don't fail the call if the
                # IMAP server doesn't expose a Sent folder we can write to.
                saved_to = None

        # Record a successful send so the next call with the same key
        # short-circuits instead of re-sending.
        if idempotency_key and self.email_cache:
            try:
                self.email_cache.record_sent(
                    idempotency_key,
                    msg.get("Message-ID"),
                    envelope_recipients,
                    subject,
                    saved_to,
                )
            except Exception as exc:
                logger_imap.warning(
                    "send_email succeeded but failed to record sent_log: %s", exc
                )

        return {
            "sent": True,
            "message_id": msg.get("Message-ID"),
            "saved_to_sent": saved_to,
            "idempotent_replay": False,
        }

    @staticmethod
    def _strip_subject_prefix(subject: str, *prefixes: str) -> str:
        """Return ``subject`` without leading prefixes (case-insensitive)."""
        s = (subject or "").strip()
        changed = True
        while changed:
            changed = False
            for p in prefixes:
                if s.lower().startswith(p.lower()):
                    s = s[len(p):].strip()
                    changed = True
        return s

    def reply_email(
        self,
        uid: int,
        body: str,
        mailbox: Optional[str] = None,
        html_body: Optional[str] = None,
        reply_all: bool = False,
        attachments: Optional[list[str]] = None,
        include_signature: bool = True,
        quote_original: bool = True,
        save_to_sent: bool = True,
        idempotency_key: Optional[str] = None,
    ) -> dict:
        """Reply to the email with the given UID.

        With ``reply_all=True``, the recipients of the original (To + Cc),
        excluding the user's own address, are added to Cc.
        """
        self._ensure_connected()
        if mailbox:
            self.select_mailbox(mailbox)

        data = self.client.fetch([uid], ["BODY[]"])
        if uid not in data:
            raise ValueError(f"Email with UID {uid} not found")
        orig = email.message_from_bytes(data[uid].get(b"BODY[]", b""))

        reply_target = orig.get("Reply-To") or orig.get("From") or ""
        _, reply_addr = email.utils.parseaddr(reply_target)
        if not reply_addr:
            raise ValueError("Original email has no usable From/Reply-To address")
        to = [reply_addr]

        cc: list[str] = []
        if reply_all:
            user_config = self.config.get("user", {})
            user_email = (
                user_config.get("email")
                or self.config.get("credentials", {}).get("username", "")
            ).lower()
            seen = {reply_addr.lower(), user_email}
            for header_value in (orig.get_all("To") or []) + (orig.get_all("Cc") or []):
                for _, addr in email.utils.getaddresses([header_value]):
                    addr_lower = (addr or "").lower()
                    if addr and addr_lower not in seen:
                        cc.append(addr)
                        seen.add(addr_lower)

        orig_subject = self._decode_header(orig.get("Subject", ""))
        bare_subject = self._strip_subject_prefix(orig_subject, "Re:", "RE:", "re:")
        subject = f"Re: {bare_subject}" if bare_subject else "Re:"

        orig_message_id = orig.get("Message-ID")
        orig_references = orig.get("References", "")
        new_references = orig_references.split() if orig_references else []
        if orig_message_id and orig_message_id not in new_references:
            new_references.append(orig_message_id)

        final_body = body
        final_html = html_body
        if quote_original:
            orig_body = self._extract_body(orig)
            from_str = self._decode_header(orig.get("From", ""))
            date_str = orig.get("Date", "")
            if orig_body.text:
                # Trim trailing whitespace per line and drop trailing blank
                # lines so the quoted block doesn't end in a sea of bare ">"
                # markers (common when the original body had a long signature
                # padded with blank lines).
                lines = [ln.rstrip() for ln in orig_body.text.splitlines()]
                while lines and not lines[-1]:
                    lines.pop()
                quoted_text = "\n".join("> " + line for line in lines)
                final_body = f"{body}\n\nOn {date_str}, {from_str} wrote:\n{quoted_text}"
            if html_body and (orig_body.html or orig_body.text):
                inner_html = orig_body.html or (orig_body.text or "").replace("\n", "<br>")
                final_html = (
                    f"{html_body}<br><br>"
                    f"<div>On {date_str}, {from_str} wrote:</div>"
                    f'<blockquote style="border-left:2px solid #ccc;'
                    f'padding-left:8px;margin-left:8px;">{inner_html}</blockquote>'
                )

        return self.send_email(
            to=to,
            subject=subject,
            body=final_body,
            cc=cc or None,
            html_body=final_html,
            attachments=attachments,
            include_signature=include_signature,
            in_reply_to=orig_message_id,
            references=new_references or None,
            save_to_sent=save_to_sent,
            idempotency_key=idempotency_key,
        )

    def forward_email(
        self,
        uid: int,
        to: list[str],
        body: str = "",
        mailbox: Optional[str] = None,
        cc: Optional[list[str]] = None,
        bcc: Optional[list[str]] = None,
        html_body: Optional[str] = None,
        include_attachments: bool = True,
        extra_attachments: Optional[list[str]] = None,
        include_signature: bool = True,
        save_to_sent: bool = True,
        idempotency_key: Optional[str] = None,
    ) -> dict:
        """Forward the email with the given UID to new recipients.

        With ``include_attachments=True`` (default), original attachments
        are re-attached to the forwarded message.
        """
        self._ensure_connected()
        if mailbox:
            self.select_mailbox(mailbox)

        data = self.client.fetch([uid], ["BODY[]"])
        if uid not in data:
            raise ValueError(f"Email with UID {uid} not found")
        orig = email.message_from_bytes(data[uid].get(b"BODY[]", b""))

        orig_subject = self._decode_header(orig.get("Subject", ""))
        bare_subject = self._strip_subject_prefix(
            orig_subject, "Fwd:", "FWD:", "Fw:", "FW:", "fwd:", "fw:"
        )
        subject = f"Fwd: {bare_subject}" if bare_subject else "Fwd:"

        from_str = self._decode_header(orig.get("From", ""))
        to_str = self._decode_header(orig.get("To", ""))
        date_str = orig.get("Date", "")
        orig_body = self._extract_body(orig)

        fwd_text_header = (
            "\n\n---------- Forwarded message ----------\n"
            f"From: {from_str}\n"
            f"Date: {date_str}\n"
            f"Subject: {orig_subject}\n"
            f"To: {to_str}\n\n"
        )
        final_body = (body or "") + fwd_text_header + (orig_body.text or "")

        final_html: Optional[str] = None
        if html_body or orig_body.html:
            html_intro = html_body or ""
            html_intro += (
                "<br><br>"
                "<div>---------- Forwarded message ----------<br>"
                f"From: {from_str}<br>"
                f"Date: {date_str}<br>"
                f"Subject: {orig_subject}<br>"
                f"To: {to_str}</div><br>"
            )
            inner_html = orig_body.html or (orig_body.text or "").replace("\n", "<br>")
            final_html = html_intro + inner_html

        attach_paths: list[str] = []
        tempdir: Optional[str] = None
        try:
            if include_attachments:
                attachments_meta = self._extract_attachment_info(orig)
                if attachments_meta:
                    tempdir = tempfile.mkdtemp(prefix="imap-fwd-")
                    for att in attachments_meta:
                        data_bytes = self._get_attachment_bytes(orig, att.index)
                        if not data_bytes:
                            continue
                        safe_name = (att.filename or f"attachment_{att.index}").replace(
                            "/", "_"
                        )
                        path = Path(tempdir) / safe_name
                        with open(path, "wb") as f:
                            f.write(data_bytes)
                        attach_paths.append(str(path))
            if extra_attachments:
                attach_paths.extend(extra_attachments)

            return self.send_email(
                to=to,
                subject=subject,
                body=final_body,
                cc=cc,
                bcc=bcc,
                html_body=final_html,
                attachments=attach_paths or None,
                include_signature=include_signature,
                save_to_sent=save_to_sent,
                idempotency_key=idempotency_key,
            )
        finally:
            if tempdir:
                shutil.rmtree(tempdir, ignore_errors=True)

    def delete_email(
        self,
        uids: list[int],
        mailbox: Optional[str] = None,
        permanent: bool = False,
        trash_folder: Optional[str] = None,
    ) -> dict:
        """Delete emails.

        Default: move to the Trash folder (configurable via ``folders.trash``,
        default ``"Trash"``). With ``permanent=True``: set the ``\\Deleted``
        flag and EXPUNGE -- skipping the Trash folder.
        """
        self._ensure_connected()
        if mailbox:
            self.select_mailbox(mailbox)

        if permanent:
            self.client.add_flags(uids, [b"\\Deleted"])
            self.client.expunge()
            return {"deleted": len(uids), "permanent": True, "moved_to": None}

        trash = trash_folder or self.config.get("folders", {}).get("trash", "Trash")
        try:
            self.client.move(uids, trash)
            moved_to = trash
        except Exception as exc:
            if (
                "namespace" in str(exc).lower()
                and not trash.startswith("INBOX.")
                and trash != "INBOX"
            ):
                prefixed = f"INBOX.{trash}"
                self.client.move(uids, prefixed)
                moved_to = prefixed
            else:
                raise
        return {"deleted": len(uids), "permanent": False, "moved_to": moved_to}

    # === HTML rendering / inline images / calendar ======================

    def get_email_body_safe(
        self,
        uid: int,
        mailbox: Optional[str] = None,
        strip_remote_images: bool = False,
        strip_links: bool = False,
        inline_cid_images: bool = True,
    ) -> dict:
        """Return a sanitized HTML body with optional inline-image inlining.

        Returns ``{"html": ..., "text": ..., "inline_images": [...]}``. The
        HTML has been passed through bleach with a conservative whitelist:
        ``<script>``, ``<style>``, ``<iframe>``, event handlers and
        ``javascript:`` URLs are removed. With ``inline_cid_images=True``
        every ``src="cid:..."`` reference is replaced by a ``data:`` URI so
        the HTML renders standalone.
        """
        self._ensure_connected()
        if mailbox:
            self.select_mailbox(mailbox)

        data = self.client.fetch([uid], ["BODY[]"])
        if uid not in data:
            raise ValueError(f"Email with UID {uid} not found")
        msg = email.message_from_bytes(data[uid].get(b"BODY[]", b""))

        body = self._extract_body(msg)
        inline = extract_inline_images(msg) if inline_cid_images else []
        html = body.html or ""
        if inline_cid_images and inline:
            html = inline_cid_to_data_uri(html, inline)
        clean = sanitize_html(
            html,
            strip_remote_images=strip_remote_images,
            strip_links=strip_links,
        ) if html else ""
        return {
            "uid": uid,
            "html": clean,
            "text": body.text or "",
            "inline_images": [
                {k: v for k, v in img.items() if k != "data_uri"}
                for img in inline
            ],
        }

    def get_email_auth_results(
        self, uid: int, mailbox: Optional[str] = None,
    ) -> dict:
        """Return SPF / DKIM / DMARC verdicts for an email.

        Parses every ``Authentication-Results`` header (RFC 8601). Useful
        for AI-assisted phishing classification: a mismatched From with
        ``spf=fail`` and ``dmarc=fail`` is a strong red flag.
        """
        self._ensure_connected()
        if mailbox:
            self.select_mailbox(mailbox)
        # Fetch only the Authentication-Results header field -- cheap.
        try:
            data = self.client.fetch(
                [uid],
                ["BODY.PEEK[HEADER.FIELDS (Authentication-Results)]"],
            )
        except Exception as exc:
            return {"error": str(exc), "uid": uid}
        if uid not in data:
            return {"error": "not found", "uid": uid}

        msg_data = data[uid]
        raw = (
            msg_data.get(b"BODY[HEADER.FIELDS (Authentication-Results)]")
            or msg_data.get(b"BODY[HEADER.FIELDS (AUTHENTICATION-RESULTS)]")
            or b""
        )
        if not raw:
            return {"uid": uid, "raw": []}

        parsed = email.message_from_bytes(raw)
        values = parsed.get_all("Authentication-Results") or []
        result = parse_authentication_results(values)
        result["uid"] = uid
        return result

    def extract_recipients_from_thread(
        self, uid: int, mailbox: Optional[str] = None,
    ) -> dict:
        """Collect every distinct address that appears in the thread.

        Returns ``{"participants": [...], "by_role": {...}, "thread_size": N}``.
        Each participant has ``email``, ``name``, ``role`` (the highest one
        seen: from > to > cc), ``message_count``. The user's own address is
        marked ``is_self: true``.
        """
        thread = self.get_thread(uid=uid, mailbox=mailbox)
        user_email = (
            self.config.get("user", {}).get("email")
            or self.config.get("credentials", {}).get("username", "")
        ).lower()

        # role priority for "highest seen": from(3) > to(2) > cc(1)
        role_priority = {"cc": 1, "to": 2, "from": 3}
        agg: dict[str, dict] = {}

        def _bump(addr: Optional[EmailAddress], role: str) -> None:
            if not addr or not addr.email:
                return
            key = addr.email.lower()
            entry = agg.setdefault(key, {
                "email": addr.email,
                "name": addr.name,
                "role": role,
                "message_count": 0,
                "is_self": key == user_email,
            })
            if not entry["name"] and addr.name:
                entry["name"] = addr.name
            if role_priority[role] > role_priority[entry["role"]]:
                entry["role"] = role
            entry["message_count"] += 1

        for h in thread:
            _bump(h.from_address, "from")
            for a in h.to_addresses:
                _bump(a, "to")
            for a in h.cc_addresses:
                _bump(a, "cc")

        participants = sorted(
            agg.values(),
            key=lambda p: (-p["message_count"], p["email"]),
        )
        by_role = {
            "from": [p for p in participants if p["role"] == "from"],
            "to": [p for p in participants if p["role"] == "to"],
            "cc": [p for p in participants if p["role"] == "cc"],
        }
        return {
            "participants": participants,
            "by_role": by_role,
            "thread_size": len(thread),
        }

    def thread_summary(
        self, uid: int, mailbox: Optional[str] = None,
    ) -> dict:
        """Produce a compact, LLM-friendly summary of the whole thread.

        Returns counts, span (oldest..newest dates), participants and a
        chronological list of {uid, subject, from, date, unread}.
        """
        thread = self.get_thread(uid=uid, mailbox=mailbox)
        if not thread:
            return {
                "thread_size": 0, "messages": [], "participants": [],
            }

        chronological = sorted(thread, key=lambda h: h.date or datetime.min)
        oldest = chronological[0].date
        newest = chronological[-1].date

        user_email = (
            self.config.get("user", {}).get("email")
            or self.config.get("credentials", {}).get("username", "")
        ).lower()

        unread_count = sum(1 for h in thread if "\\Seen" not in h.flags)
        from_self = sum(
            1 for h in thread
            if h.from_address and h.from_address.email.lower() == user_email
        )
        recipients_info = self.extract_recipients_from_thread(uid=uid, mailbox=mailbox)

        # Use the most-recent non-Re/Fwd subject as the canonical thread title.
        title = next(
            (
                h.subject for h in reversed(chronological)
                if h.subject
            ), None
        ) or "(no subject)"

        messages = [
            {
                "uid": h.uid,
                "subject": h.subject,
                "from": h.from_address.email if h.from_address else None,
                "from_name": h.from_address.name if h.from_address else None,
                "date": h.date.isoformat() if h.date else None,
                "unread": "\\Seen" not in h.flags,
                "from_self": (
                    h.from_address is not None
                    and h.from_address.email.lower() == user_email
                ),
            }
            for h in chronological
        ]
        return {
            "title": title,
            "thread_size": len(thread),
            "unread_count": unread_count,
            "messages_from_self": from_self,
            "span": {
                "oldest": oldest.isoformat() if oldest else None,
                "newest": newest.isoformat() if newest else None,
            },
            "participants": recipients_info["participants"],
            "messages": messages,
        }

    def get_calendar_invites(
        self, uid: int, mailbox: Optional[str] = None
    ) -> list[dict]:
        """Return parsed VEVENTs from any ``text/calendar`` parts of an email."""
        self._ensure_connected()
        if mailbox:
            self.select_mailbox(mailbox)

        data = self.client.fetch([uid], ["BODY[]"])
        if uid not in data:
            raise ValueError(f"Email with UID {uid} not found")
        msg = email.message_from_bytes(data[uid].get(b"BODY[]", b""))
        return parse_calendar_invites(msg)

    # === AI-friendly summaries ==========================================

    def get_email_summary(
        self,
        uids: list[int],
        mailbox: Optional[str] = None,
        body_chars: int = 300,
        peek_bytes: Optional[int] = None,
    ) -> list[dict]:
        """Return a compact summary list for ``uids`` -- cheap LLM-friendly.

        Each entry has subject, sender, date, flags, size, has_attachments
        and the first ``body_chars`` of the plain-text body. Bodies are
        served from cache when available so a 50-email overview costs zero
        IMAP body fetches the second time around.

        For uncached UIDs, only the first ``peek_bytes`` of the message text
        are fetched via IMAP partial FETCH (``BODY.PEEK[TEXT]<0.N>``,
        RFC 3501) -- defaults to ``max(body_chars * 4, 1024)`` so the
        snippet has enough material even after MIME decoding overhead. Pass
        ``peek_bytes=0`` to skip the body fetch entirely (only headers).
        """
        self._ensure_connected()
        if mailbox:
            self.select_mailbox(mailbox)
        effective_mailbox = mailbox or self.current_mailbox or "INBOX"

        if not uids:
            return []

        if peek_bytes is None:
            peek_bytes = max(body_chars * 4, 1024)

        # Split: which uids do we already have cached?
        cached_summaries: dict[int, dict] = {}
        uids_need_body: list[int] = []
        if self.email_cache:
            for u in uids:
                row = self.email_cache.get_email(effective_mailbox, u)
                if row and row.get("has_body"):
                    cached_summaries[u] = self._row_to_summary(row, body_chars)
                else:
                    uids_need_body.append(u)
        else:
            uids_need_body = list(uids)

        if uids_need_body:
            # One round trip with BODYSTRUCTURE so we know who has attachments
            # and a *partial* BODY.PEEK[TEXT] so the snippet costs ~1 KiB per
            # email instead of the whole body.
            body_field: Optional[str]
            if peek_bytes and peek_bytes > 0:
                body_field = f"BODY.PEEK[TEXT]<0.{int(peek_bytes)}>"
            else:
                body_field = None

            fields = ["ENVELOPE", "FLAGS", "RFC822.SIZE", "BODYSTRUCTURE"]
            if body_field:
                fields.append(body_field)
            messages = self.client.fetch(uids_need_body, fields)
            for u, d in messages.items():
                hdr = self._parse_email_header(u, d)
                bs = d.get(b"BODYSTRUCTURE")
                has_att = self._bodystructure_has_attachment(bs)
                raw_text = b""
                if body_field:
                    # Servers may use either the parameterized key or the bare
                    # key in the response; try both.
                    for key in (
                        f"BODY[TEXT]<0>".encode(),
                        b"BODY[TEXT]",
                        body_field.replace("BODY.PEEK", "BODY").encode(),
                    ):
                        if key in d:
                            raw_text = d[key] or b""
                            break
                text = raw_text.decode("utf-8", errors="replace") if raw_text else ""
                cached_summaries[u] = {
                    "uid": u,
                    "subject": hdr.subject,
                    "sender": (hdr.from_address.email if hdr.from_address else None),
                    "sender_name": (hdr.from_address.name if hdr.from_address else None),
                    "date": hdr.date.isoformat() if hdr.date else None,
                    "flags": hdr.flags,
                    "unread": "\\Seen" not in hdr.flags,
                    "size": hdr.size,
                    "has_attachments": has_att,
                    "snippet": (text[:body_chars] + "…") if len(text) > body_chars else text,
                }

        # Preserve input order.
        return [cached_summaries[u] for u in uids if u in cached_summaries]

    @staticmethod
    def _row_to_summary(row: dict, body_chars: int) -> dict:
        """Build a summary dict directly from a cached emails row."""
        text = row.get("body_text") or ""
        flags = []
        try:
            flags = json.loads(row.get("flags") or "[]")
        except (TypeError, ValueError):
            flags = []
        return {
            "uid": row["uid"],
            "subject": row.get("subject"),
            "sender": row.get("from_email"),
            "sender_name": row.get("from_name"),
            "date": row.get("date"),
            "flags": flags,
            "unread": "\\Seen" not in flags,
            "size": row.get("size"),
            "has_attachments": False,  # cache layer doesn't track this directly
            "snippet": (text[:body_chars] + "…") if len(text) > body_chars else text,
        }

    # === Bulk operations by query =======================================

    BULK_ACTIONS = {
        "mark_read", "mark_unread", "flag", "unflag", "archive", "delete",
        "move", "copy", "report_spam",
    }

    def bulk_action(
        self,
        action: str,
        query: Optional[list] = None,
        mailbox: Optional[str] = None,
        from_addr: Optional[str] = None,
        subject: Optional[str] = None,
        since: Optional[str] = None,
        before: Optional[str] = None,
        unread: Optional[bool] = None,
        flagged: Optional[bool] = None,
        # Action-specific
        destination: Optional[str] = None,
        flag_name: Optional[str] = None,
        permanent: bool = False,
        dry_run: bool = False,
        limit: Optional[int] = None,
        batch_size: int = 1000,
    ) -> dict:
        """Apply ``action`` to every message matching the search criteria.

        ``action`` is one of :data:`BULK_ACTIONS`. Search criteria mirror the
        parameters of :meth:`search_advanced` (in addition to a free-form
        ``query`` list of IMAP SEARCH tokens). With ``dry_run=True`` the
        message is matched but no mutation is performed -- useful as a
        preview.

        ``limit`` caps the number of matches that get acted on (oldest-first
        by UID). The full match count is still reported. ``batch_size``
        chunks the IMAP command to avoid overrunning command-length limits
        on large UID sets (some servers reject 50 K-element UID lists in a
        single STORE/MOVE).

        Returns ``{"action", "matched", "affected", "uids", "dry_run",
        "truncated", "batch_size", ...}``.
        """
        if action not in self.BULK_ACTIONS:
            raise ValueError(
                f"Unknown bulk action: {action!r}. Allowed: {sorted(self.BULK_ACTIONS)}"
            )
        if limit is not None and limit < 0:
            raise ValueError("limit must be >= 0")
        if batch_size <= 0:
            raise ValueError("batch_size must be > 0")

        self._ensure_connected()
        if mailbox:
            self.select_mailbox(mailbox)
        elif not self.current_mailbox:
            self.select_mailbox("INBOX")

        criteria: list = list(query) if query else []
        if unread is True:
            criteria.append("UNSEEN")
        elif unread is False:
            criteria.append("SEEN")
        if flagged is True:
            criteria.append("FLAGGED")
        elif flagged is False:
            criteria.append("UNFLAGGED")
        if from_addr:
            criteria.extend(["FROM", from_addr])
        if subject:
            criteria.extend(["SUBJECT", subject])
        if since:
            criteria.extend(["SINCE", self._to_imap_date(since)])
        if before:
            criteria.extend(["BEFORE", self._to_imap_date(before)])
        if not criteria:
            criteria = ["ALL"]

        try:
            uids = self.client.search(criteria, charset="UTF-8")
        except Exception:
            uids = self.client.search(criteria)

        matched = len(uids)
        truncated = False
        if limit is not None and matched > limit:
            uids = sorted(uids)[:limit]
            truncated = True

        if not uids:
            return {
                "action": action, "matched": matched, "affected": 0,
                "uids": [], "dry_run": dry_run,
                "mailbox": self.current_mailbox,
                "truncated": truncated, "batch_size": batch_size,
            }

        target_uids = list(uids)

        result: dict = {
            "action": action,
            "matched": matched,
            "uids": target_uids,
            "dry_run": dry_run,
            "mailbox": self.current_mailbox,
            "truncated": truncated,
            "batch_size": batch_size,
        }

        if dry_run:
            result["affected"] = 0
            return result

        def _chunks(seq: list[int]) -> list[list[int]]:
            return [seq[i:i + batch_size] for i in range(0, len(seq), batch_size)]

        if action == "mark_read":
            for chunk in _chunks(target_uids):
                self.client.add_flags(chunk, [b"\\Seen"])
        elif action == "mark_unread":
            for chunk in _chunks(target_uids):
                self.client.remove_flags(chunk, [b"\\Seen"])
        elif action == "flag":
            flag_b = (flag_name or "\\Flagged").encode()
            for chunk in _chunks(target_uids):
                self.client.add_flags(chunk, [flag_b])
        elif action == "unflag":
            flag_b = (flag_name or "\\Flagged").encode()
            for chunk in _chunks(target_uids):
                self.client.remove_flags(chunk, [flag_b])
        elif action == "archive":
            folder = destination or self.config.get("folders", {}).get("archive", "Archive")
            for chunk in _chunks(target_uids):
                self.archive_email(uids=chunk, archive_folder=folder)
        elif action == "move":
            if not destination:
                raise ValueError("bulk_action(action='move') requires 'destination'.")
            for chunk in _chunks(target_uids):
                self.move_email(uids=chunk, destination=destination)
        elif action == "copy":
            if not destination:
                raise ValueError("bulk_action(action='copy') requires 'destination'.")
            for chunk in _chunks(target_uids):
                self.copy_email(uids=chunk, destination=destination)
        elif action == "delete":
            last: dict = {}
            for chunk in _chunks(target_uids):
                last = self.delete_email(
                    uids=chunk,
                    permanent=permanent,
                    trash_folder=destination,
                )
            result.update({k: last[k] for k in ("permanent", "moved_to") if k in last})
        elif action == "report_spam":
            last = {}
            for chunk in _chunks(target_uids):
                last = self.report_spam(uids=chunk, spam_folder=destination)
            result.update({k: last[k] for k in ("moved_to", "flag") if k in last})

        result["affected"] = len(target_uids)
        return result

    # === Account health =================================================

    def health_check(self) -> dict:
        """Light-weight reachability check for this account.

        Tries to issue a NOOP if connected, otherwise just reports status.
        Doesn't open a new connection -- intended to be called by the
        ``accounts_health`` server-level tool, which inspects every account
        without forcing a connect.
        """
        if not self.client:
            return {
                "connected": False,
                "ok": False,
                "reason": "not connected",
            }
        try:
            self.client.noop()
        except Exception as exc:
            return {
                "connected": True,
                "ok": False,
                "reason": str(exc),
            }
        cache_stats = None
        if self.email_cache:
            try:
                cache_stats = {
                    "emails_cached": self.email_cache.get_cached_count("INBOX"),
                    "encrypted": self.email_cache.encrypted,
                }
            except Exception:
                cache_stats = None
        return {
            "connected": True,
            "ok": True,
            "current_mailbox": self.current_mailbox,
            "watching": self.watching,
            "cache": cache_stats,
        }

    # === Draft management ================================================

    def update_draft(
        self,
        uid: int,
        to: list[str],
        subject: str,
        body: str,
        cc: Optional[list[str]] = None,
        bcc: Optional[list[str]] = None,
        html_body: Optional[str] = None,
        attachments: Optional[list[str]] = None,
        drafts_folder: str = "Drafts",
        include_signature: bool = True,
    ) -> dict:
        """Replace an existing draft.

        IMAP drafts are immutable, so this APPENDs the new draft and then
        \\Deleted+EXPUNGEs the original UID. Returns the new UID when the
        server reports it via APPENDUID, otherwise just confirms the swap.
        """
        self._ensure_connected()

        msg = self._build_message(
            to=to, subject=subject, body=body,
            cc=cc, bcc=bcc, html_body=html_body,
            attachments=attachments,
            include_signature=include_signature,
            include_bcc_header=True,
        )

        new_uid: Optional[int] = None
        try:
            append_resp = self.client.append(
                drafts_folder, msg.as_bytes(), flags=[b"\\Draft"]
            )
            actual_folder = drafts_folder
        except Exception as e:
            err = str(e).lower()
            if (
                ("namespace" in err or "no such" in err or "mailbox" in err)
                and not drafts_folder.startswith("INBOX.")
                and drafts_folder != "INBOX"
            ):
                actual_folder = f"INBOX.{drafts_folder}"
                append_resp = self.client.append(
                    actual_folder, msg.as_bytes(), flags=[b"\\Draft"]
                )
            else:
                raise

        # IMAPClient may return the assigned UID via APPENDUID.
        if isinstance(append_resp, (bytes, str)):
            try:
                # Format: "[APPENDUID <uidvalidity> <uid>] (Success)"
                text = append_resp.decode() if isinstance(append_resp, bytes) else append_resp
                if "APPENDUID" in text.upper():
                    parts = text.split()
                    for i, p in enumerate(parts):
                        if p.upper().endswith("APPENDUID") and i + 2 < len(parts):
                            new_uid = int(parts[i + 2].rstrip("]"))
                            break
            except (ValueError, AttributeError):
                pass

        # Delete the old draft.
        self.select_mailbox(actual_folder)
        self.client.add_flags([uid], [b"\\Deleted"])
        self.client.expunge()

        return {
            "updated": True,
            "old_uid": uid,
            "new_uid": new_uid,
            "drafts_folder": actual_folder,
        }

    def delete_draft(
        self, uid: int, drafts_folder: str = "Drafts"
    ) -> dict:
        """Permanently remove one draft from the Drafts folder."""
        self._ensure_connected()
        try:
            self.select_mailbox(drafts_folder)
        except Exception as e:
            err = str(e).lower()
            if "namespace" in err or "no such" in err or "mailbox" in err:
                self.select_mailbox(f"INBOX.{drafts_folder}")
            else:
                raise
        self.client.add_flags([uid], [b"\\Deleted"])
        self.client.expunge()
        return {"deleted": True, "uid": uid, "drafts_folder": self.current_mailbox}

    # === Spam ============================================================

    def report_spam(
        self,
        uids: list[int],
        mailbox: Optional[str] = None,
        spam_folder: Optional[str] = None,
        flag: Optional[str] = None,
    ) -> dict:
        """Mark messages as spam: add the junk flag and move to the Spam folder.

        Folder defaults to ``folders.spam`` (or ``"Spam"``). Junk flag
        defaults to ``spam.junk_flag`` (or ``"$Junk"``). Either can be
        disabled by passing an empty string.
        """
        self._ensure_connected()
        if mailbox:
            self.select_mailbox(mailbox)

        spam_cfg = self.config.get("spam", {})
        target = spam_folder or self.config.get("folders", {}).get("spam", "Spam")
        junk_flag = (
            flag if flag is not None else spam_cfg.get("junk_flag", "$Junk")
        )
        not_junk_flag = spam_cfg.get("not_junk_flag", "$NotJunk")

        if junk_flag:
            try:
                self.client.add_flags(uids, [junk_flag.encode()])
            except Exception as exc:
                logger_imap.debug("Could not add %s flag: %s", junk_flag, exc)
            if not_junk_flag:
                try:
                    self.client.remove_flags(uids, [not_junk_flag.encode()])
                except Exception:
                    pass

        try:
            self.client.move(uids, target)
            moved_to = target
        except Exception as exc:
            if (
                "namespace" in str(exc).lower()
                and not target.startswith("INBOX.")
                and target != "INBOX"
            ):
                moved_to = f"INBOX.{target}"
                self.client.move(uids, moved_to)
            else:
                raise
        return {"reported": len(uids), "moved_to": moved_to, "flag": junk_flag or None}

    def mark_not_spam(
        self,
        uids: list[int],
        mailbox: Optional[str] = None,
        destination: Optional[str] = None,
    ) -> dict:
        """Move messages out of the Spam folder and clear the junk flag."""
        self._ensure_connected()
        spam_folder = mailbox or self.config.get("folders", {}).get("spam", "Spam")
        try:
            self.select_mailbox(spam_folder)
        except Exception as exc:
            err = str(exc).lower()
            if "namespace" in err or "no such" in err or "mailbox" in err:
                self.select_mailbox(f"INBOX.{spam_folder}")
            else:
                raise

        spam_cfg = self.config.get("spam", {})
        junk_flag = spam_cfg.get("junk_flag", "$Junk")
        not_junk_flag = spam_cfg.get("not_junk_flag", "$NotJunk")

        if junk_flag:
            try:
                self.client.remove_flags(uids, [junk_flag.encode()])
            except Exception:
                pass
        if not_junk_flag:
            try:
                self.client.add_flags(uids, [not_junk_flag.encode()])
            except Exception:
                pass

        target = destination or self.config.get("folders", {}).get("inbox", "INBOX")
        try:
            self.client.move(uids, target)
            moved_to = target
        except Exception as exc:
            if (
                "namespace" in str(exc).lower()
                and not target.startswith("INBOX.")
                and target != "INBOX"
            ):
                moved_to = f"INBOX.{target}"
                self.client.move(uids, moved_to)
            else:
                raise
        return {"unspammed": len(uids), "moved_to": moved_to}

    # === FTS-aware search ================================================

    def search_emails_fts(
        self,
        query: str,
        mailbox: Optional[str] = None,
        limit: int = 50,
    ) -> list[EmailHeader]:
        """Run an FTS5 query against the local cache.

        Requires that the cache is populated via :meth:`load_cache`. Returns
        :class:`EmailHeader` items reconstructed from cache rows -- no IMAP
        round trip.
        """
        if not self.email_cache:
            raise RuntimeError(
                "Persistent cache is disabled. Set cache.enabled=true and "
                "load emails with load_cache() before using FTS."
            )
        rows = self.email_cache.fts_search(query, mailbox=mailbox, limit=limit)
        return [self._cached_to_header(r) for r in rows]

    def search_advanced(
        self,
        query: Optional[str] = None,
        from_addr: Optional[str] = None,
        to_addr: Optional[str] = None,
        subject: Optional[str] = None,
        since: Optional[str] = None,
        before: Optional[str] = None,
        has_attachments: Optional[bool] = None,
        unread: Optional[bool] = None,
        flagged: Optional[bool] = None,
        mailbox: Optional[str] = None,
        use_fts: bool = False,
        limit: int = 50,
    ) -> list[EmailHeader]:
        """Combine multiple IMAP SEARCH criteria in a single call.

        With ``use_fts=True`` (or when there's no IMAP connection but the
        cache is available), the body part of ``query`` is matched via FTS5
        and the structured criteria are applied as a post-filter on results.

        Otherwise uses the IMAP server's SEARCH with combined criteria.
        """
        if use_fts:
            if not query:
                raise ValueError("FTS mode requires 'query'")
            results = self.search_emails_fts(query, mailbox=mailbox, limit=max(limit * 4, limit))
            # Apply structured filters in Python
            def _match(h: EmailHeader) -> bool:
                if from_addr:
                    fa = h.from_address
                    haystack = ((fa.email if fa else "") + " " + (fa.name or "" if fa else "")).lower()
                    if from_addr.lower() not in haystack:
                        return False
                if subject and (subject.lower() not in (h.subject or "").lower()):
                    return False
                if to_addr:
                    addrs = " ".join(a.email + " " + (a.name or "") for a in h.to_addresses).lower()
                    if to_addr.lower() not in addrs:
                        return False
                return True
            return [h for h in results if _match(h)][:limit]

        self._ensure_connected()
        if mailbox:
            self.select_mailbox(mailbox)
        elif not self.current_mailbox:
            self.select_mailbox("INBOX")

        criteria: list = []
        if unread is True:
            criteria.append("UNSEEN")
        elif unread is False:
            criteria.append("SEEN")
        if flagged is True:
            criteria.append("FLAGGED")
        elif flagged is False:
            criteria.append("UNFLAGGED")
        if from_addr:
            criteria.extend(["FROM", from_addr])
        if to_addr:
            criteria.extend(["TO", to_addr])
        if subject:
            criteria.extend(["SUBJECT", subject])
        if query:
            criteria.extend(["TEXT", query])
        if since:
            criteria.extend(["SINCE", self._to_imap_date(since)])
        if before:
            criteria.extend(["BEFORE", self._to_imap_date(before)])

        if not criteria:
            criteria = ["ALL"]

        try:
            uids = self.client.search(criteria, charset="UTF-8")
        except Exception:
            uids = self.client.search(criteria)

        if has_attachments is not None:
            # Filter via BODYSTRUCTURE (cheap-ish)
            uids_to_check = sorted(uids, reverse=True)[:limit * 4]
            keep = []
            if uids_to_check:
                msgs = self.client.fetch(uids_to_check, ["BODYSTRUCTURE"])
                for u, d in msgs.items():
                    bs = d.get(b"BODYSTRUCTURE")
                    has = self._bodystructure_has_attachment(bs)
                    if has == has_attachments:
                        keep.append(u)
            uids = keep

        uids = sorted(uids, reverse=True)[:limit]
        if not uids:
            return []
        messages = self.client.fetch(uids, ["ENVELOPE", "FLAGS", "RFC822.SIZE"])
        return [self._parse_email_header(uid, data) for uid, data in messages.items()]

    @staticmethod
    def _bodystructure_has_attachment(bs) -> bool:
        """Best-effort check whether a BODYSTRUCTURE indicates attachments."""
        if bs is None:
            return False
        # Multipart: a tuple where the first element is itself a tuple/list
        try:
            if isinstance(bs, (list, tuple)) and bs and isinstance(bs[0], (list, tuple)):
                # Walk children
                for child in bs:
                    if isinstance(child, (list, tuple)) and child and isinstance(child[0], (list, tuple)):
                        if ImapClientWrapper._bodystructure_has_attachment(child):
                            return True
                    elif isinstance(child, (list, tuple)):
                        # Leaf part: check Content-Disposition slot
                        disposition_slot = child[8] if len(child) > 8 else None
                        if disposition_slot and isinstance(disposition_slot, (list, tuple)):
                            disp = disposition_slot[0]
                            disp = disp.decode() if isinstance(disp, bytes) else (disp or "")
                            if "attachment" in disp.lower():
                                return True
                return False
            # Singlepart leaf
            if isinstance(bs, (list, tuple)) and len(bs) > 8:
                disposition_slot = bs[8]
                if disposition_slot and isinstance(disposition_slot, (list, tuple)):
                    disp = disposition_slot[0]
                    disp = disp.decode() if isinstance(disp, bytes) else (disp or "")
                    if "attachment" in disp.lower():
                        return True
        except Exception:
            return False
        return False

    def rebuild_search_index(self, mailbox: Optional[str] = None) -> dict:
        """Rebuild the FTS5 index from the persistent cache."""
        if not self.email_cache:
            raise RuntimeError("Persistent cache is disabled.")
        count = self.email_cache.rebuild_fts(mailbox=mailbox)
        return {"indexed": count, "mailbox": mailbox or "<all>"}

    # === Statistics ===

    def get_unread_count(self, mailbox: str = "INBOX") -> int:
        """Get count of unread emails."""
        self._ensure_connected()
        status = self.client.folder_status(mailbox, ["UNSEEN"])
        return self._parse_status_value(status.get(b"UNSEEN", 0))

    def get_total_count(self, mailbox: str = "INBOX") -> int:
        """Get total email count in mailbox."""
        self._ensure_connected()
        status = self.client.folder_status(mailbox, ["MESSAGES"])
        return self._parse_status_value(status.get(b"MESSAGES", 0))

    # === Cache & Watch ===

    def get_cached_overview(
        self, mailbox: Optional[str] = None, limit: int = 20
    ) -> dict:
        """Get cached email overview for INBOX, next, waiting, someday (from in-memory cache)."""
        # If watcher is running, use its cache
        if self.watcher and self.watcher.running:
            # Wait briefly for cache to populate if empty
            for _ in range(10):
                cache = self.watcher.get_cache(mailbox)
                if cache:
                    # Apply limit to the emails list in each mailbox
                    if limit:
                        for key in cache:
                            if isinstance(cache[key], dict) and "emails" in cache[key]:
                                cache[key]["emails"] = cache[key]["emails"][:limit]
                    return cache
                time.sleep(0.5)
            cache = self.watcher.get_cache(mailbox)
            if limit and cache:
                for key in cache:
                    if isinstance(cache[key], dict) and "emails" in cache[key]:
                        cache[key]["emails"] = cache[key]["emails"][:limit]
            return cache

        # Fallback to manual fetch if watcher not running
        folders = self.config.get("folders", {})
        mailboxes = {
            "inbox": folders.get("inbox", "INBOX"),
            "next": folders.get("next", "next"),
            "waiting": folders.get("waiting", "waiting"),
            "someday": folders.get("someday", "someday"),
        }

        if mailbox:
            if mailbox not in mailboxes:
                return {}
            mailboxes = {mailbox: mailboxes[mailbox]}

        result = {}
        for key, folder in mailboxes.items():
            cache_key = f"overview_{folder}"
            if cache_key in self.cache:
                ttl = self.config.get("cache", {}).get("ttl_seconds", 300)
                cache_time = self.cache_timestamps.get(cache_key, datetime.min)
                if (datetime.now() - cache_time).total_seconds() < ttl:
                    result[key] = self.cache[cache_key]
                    continue

            try:
                emails = self.fetch_emails(folder, limit=limit)
                status = self.get_mailbox_status(folder)
                overview = {
                    "emails": [
                        {
                            "uid": e.uid,
                            "sender": e.from_address.email if e.from_address else "",
                            "sender_name": e.from_address.name if e.from_address else None,
                            "subject": e.subject,
                            "date": e.date.isoformat() if e.date else None,
                            "unread": "\\Seen" not in e.flags,
                        }
                        for e in emails
                    ],
                    "total": status.exists,
                    "unread": status.unseen,
                    "last_updated": datetime.now().isoformat(),
                }
                self.cache[cache_key] = overview
                self.cache_timestamps[cache_key] = datetime.now()
                result[key] = overview
            except Exception as e:
                result[key] = {"error": str(e)}

        return result

    def refresh_cache(self) -> bool:
        """Force refresh of email cache for all watched mailboxes."""
        if self.watcher and self.watcher.running:
            self.watcher.refresh()
        else:
            self.cache.clear()
            self.cache_timestamps.clear()
            self.get_cached_overview()
        return True

    def start_watch(self) -> bool:
        """Start permanent IDLE watch on INBOX, next, waiting, someday."""
        if not self.watcher:
            self.watcher = ImapWatcher(
                config_path=self.config.get("_config_path"),
                config=self.config,
            )
        self.watcher.start()
        self.watching = True
        return True

    def stop_watch(self) -> bool:
        """Stop the permanent IDLE watch."""
        if self.watcher:
            self.watcher.stop()
        self.watching = False
        return True

    def idle_watch(
        self, mailbox: str = "INBOX", timeout: int = 300
    ) -> dict:
        """Start watching mailbox for new emails (IMAP IDLE) - single mailbox, temporary."""
        self._ensure_connected()
        self.select_mailbox(mailbox)

        self.client.idle()
        responses = self.client.idle_check(timeout=timeout)
        self.client.idle_done()

        return {
            "mailbox": mailbox,
            "responses": [str(r) for r in responses],
        }

    # === Auto-Archive ===

    def get_auto_archive_list(self) -> list[AutoArchiveSender]:
        """Get list of senders that are auto-archived."""
        return self.auto_archive_senders

    def add_auto_archive_sender(
        self, email_addr: str, comment: Optional[str] = None
    ) -> bool:
        """Add sender to auto-archive list."""
        sender = AutoArchiveSender(
            email=email_addr,
            comment=comment,
            added_at=datetime.now(),
        )
        self.auto_archive_senders.append(sender)
        self._save_auto_archive_config()
        return True

    def remove_auto_archive_sender(self, email_addr: str) -> bool:
        """Remove sender from auto-archive list."""
        self.auto_archive_senders = [
            s for s in self.auto_archive_senders if s.email != email_addr
        ]
        self._save_auto_archive_config()
        return True

    def reload_auto_archive(self) -> bool:
        """Reload auto-archive config from file."""
        self._load_auto_archive_config()
        return True

    def _save_auto_archive_config(self):
        """Save auto-archive sender list to file."""
        aa_config = self.config.get("auto_archive", {})
        senders_file = aa_config.get("senders_file", "auto_archive_senders.json")

        data = {
            "senders": [s.model_dump() for s in self.auto_archive_senders]
        }
        # Convert datetime to string
        for s in data["senders"]:
            if isinstance(s.get("added_at"), datetime):
                s["added_at"] = s["added_at"].isoformat()

        with open(senders_file, "w") as f:
            json.dump(data, f, indent=2, default=str)

    def process_auto_archive(self, dry_run: bool = False) -> dict:
        """Process INBOX and archive emails from listed senders.

        Args:
            dry_run: If True, only report what would be archived without moving.

        Returns:
            dict with 'archived_count', 'archived_emails', and 'errors'.
        """
        self._ensure_connected()

        if not self.auto_archive_senders:
            return {"archived_count": 0, "archived_emails": [], "errors": [], "dry_run": dry_run, "message": "No senders in auto-archive list"}

        # Build set of sender emails/domains for fast lookup
        sender_patterns = set()
        for s in self.auto_archive_senders:
            sender_patterns.add(s.email.lower())

        # Get archive folder from config
        folders = self.config.get("folders", {})
        archive_folder = folders.get("archive", "Archive")
        inbox_folder = folders.get("inbox", "INBOX")

        # Select INBOX
        self.client.select_folder(inbox_folder)

        # Search all emails
        uids = self.client.search(["ALL"])
        if not uids:
            return {"archived_count": 0, "archived_emails": [], "errors": [], "dry_run": dry_run, "message": "INBOX is empty"}

        # Fetch envelopes to check senders (in batches of 500 to avoid
        # exceeding server command-length limits on large mailboxes)
        messages = {}
        for i in range(0, len(uids), 500):
            batch = uids[i:i + 500]
            messages.update(self.client.fetch(batch, ["ENVELOPE"]))

        to_archive = []
        archived_emails = []
        errors = []

        for uid, data in messages.items():
            envelope = data.get(b"ENVELOPE")
            if not envelope or not envelope.from_:
                continue

            # Get sender email
            f = envelope.from_[0]
            mailbox = f.mailbox.decode() if f.mailbox else ""
            host = f.host.decode() if f.host else ""
            sender_email = f"{mailbox}@{host}".lower()
            sender_domain = f"@{host}".lower()

            # Check if sender matches
            if sender_email in sender_patterns or sender_domain in sender_patterns:
                subject = ""
                if envelope.subject:
                    try:
                        subject = envelope.subject.decode("utf-8", errors="replace")
                    except Exception:
                        subject = str(envelope.subject)

                to_archive.append(uid)
                archived_emails.append({
                    "uid": uid,
                    "sender": sender_email,
                    "subject": subject[:100],
                })

        # Move emails if not dry run -- batched to avoid huge UID-list
        # commands that can blow IMAP command-length limits on large
        # mailboxes (50K UIDs in one MOVE is rejected by some servers).
        if to_archive and not dry_run:
            batch_size = 1000
            for i in range(0, len(to_archive), batch_size):
                chunk = to_archive[i:i + batch_size]
                try:
                    self.client.move(chunk, archive_folder)
                except Exception as e:
                    errors.append(
                        f"Failed to move batch starting at index {i}: {e}"
                    )

        return {
            "archived_count": len(to_archive),
            "archived_emails": archived_emails,
            "errors": errors,
            "dry_run": dry_run,
            "message": f"{'Would archive' if dry_run else 'Archived'} {len(to_archive)} emails",
        }
