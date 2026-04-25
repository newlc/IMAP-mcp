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
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import keyring
from imapclient import IMAPClient

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
from .watcher import ImapWatcher, get_watcher

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
    """Wrapper around IMAPClient for MCP operations."""

    def __init__(self):
        self.client: Optional[IMAPClient] = None
        self.config: dict = {}
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
        """Establish IMAP connection."""
        self.client = IMAPClient(host, port=port, ssl=secure)
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

        # Initialize persistent SQLite cache
        cache_config = self.config.get("cache", {})
        db_path = cache_config.get("db_path", "~/.imap-mcp/cache.db")
        encrypt = cache_config.get("encrypt", False)
        self.email_cache = EmailCache(db_path, encrypted=encrypt)

        # Auto-start watcher if cache is enabled
        if cache_config.get("enabled", True):
            self.watcher = get_watcher(config_path)
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

    def list_mailboxes(self, pattern: str = "*") -> list[MailboxInfo]:
        """List all mailbox folders."""
        self._ensure_connected()
        folders = self.client.list_folders(pattern=pattern)
        return [
            MailboxInfo(
                name=f[2],
                delimiter=f[1],
                flags=[str(flag) for flag in f[0]],
            )
            for f in folders
        ]

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
        """Get email thread/conversation."""
        self._ensure_connected()
        if mailbox:
            self.select_mailbox(mailbox)

        # Get the email to find its references
        email_data = self.get_email(uid, mailbox)
        message_id = email_data.header.message_id
        subject = email_data.header.subject

        # Search for related emails by subject (simplified thread detection)
        if subject:
            # Remove Re: Fwd: etc. prefixes
            clean_subject = subject
            for prefix in ["Re:", "RE:", "Fwd:", "FWD:", "Fw:", "AW:", "Aw:"]:
                clean_subject = clean_subject.replace(prefix, "").strip()

            uids = self.client.search(["SUBJECT", clean_subject], charset="UTF-8")
            if uids:
                messages = self.client.fetch(uids, ["ENVELOPE", "FLAGS", "RFC822.SIZE"])
                headers = [self._parse_email_header(u, d) for u, d in messages.items()]
                return sorted(headers, key=lambda h: h.date or datetime.min)

        return [email_data.header]

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

    def save_draft(
        self,
        to: list[str],
        subject: str,
        body: str,
        cc: Optional[list[str]] = None,
        bcc: Optional[list[str]] = None,
        html_body: Optional[str] = None,
        drafts_folder: str = "Drafts",
        include_signature: bool = True,
    ) -> bool:
        """Save email as draft."""
        self._ensure_connected()

        from email.mime.text import MIMEText
        from email.mime.multipart import MIMEMultipart

        # Append signature if enabled
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

        # Set From header from config
        user_config = self.config.get("user", {})
        from_name = user_config.get("name", "")
        from_email = user_config.get("email", self.config.get("credentials", {}).get("username", ""))

        if final_html:
            msg = MIMEMultipart("alternative")
            msg.attach(MIMEText(final_body, "plain"))
            msg.attach(MIMEText(final_html, "html"))
        else:
            msg = MIMEText(final_body)

        msg["To"] = ", ".join(to)
        msg["Subject"] = subject
        if from_name and from_email:
            msg["From"] = f"{from_name} <{from_email}>"
        elif from_email:
            msg["From"] = from_email
        if cc:
            msg["Cc"] = ", ".join(cc)
        if bcc:
            msg["Bcc"] = ", ".join(bcc)
        msg["Date"] = email.utils.formatdate(localtime=True)

        try:
            self.client.append(
                drafts_folder,
                msg.as_bytes(),
                flags=[b"\\Draft"],
            )
        except Exception as e:
            # Retry with INBOX. namespace prefix (e.g. Jino servers)
            if "namespace" in str(e).lower() or "no such" in str(e).lower() or "mailbox" in str(e).lower():
                prefixed = f"INBOX.{drafts_folder}"
                self.client.append(
                    prefixed,
                    msg.as_bytes(),
                    flags=[b"\\Draft"],
                )
            else:
                raise
        return True

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
            config_path = Path(self.config.get("_config_path", "config.json"))
            self.watcher = get_watcher(str(config_path))

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

        # Move emails if not dry run
        if to_archive and not dry_run:
            try:
                self.client.move(to_archive, archive_folder)
            except Exception as e:
                errors.append(f"Failed to move emails: {str(e)}")

        return {
            "archived_count": len(to_archive),
            "archived_emails": archived_emails,
            "errors": errors,
            "dry_run": dry_run,
            "message": f"{'Would archive' if dry_run else 'Archived'} {len(to_archive)} emails",
        }
