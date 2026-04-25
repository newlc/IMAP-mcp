"""Pydantic models for IMAP MCP Server."""

from datetime import datetime
from typing import Optional
from pydantic import BaseModel


class EmailAddress(BaseModel):
    """Email address with optional name."""
    name: Optional[str] = None
    email: str


class EmailHeader(BaseModel):
    """Email header information."""
    uid: int
    message_id: Optional[str] = None
    subject: Optional[str] = None
    from_address: Optional[EmailAddress] = None
    to_addresses: list[EmailAddress] = []
    cc_addresses: list[EmailAddress] = []
    date: Optional[datetime] = None
    flags: list[str] = []
    size: Optional[int] = None


class EmailBody(BaseModel):
    """Email body content."""
    text: Optional[str] = None
    html: Optional[str] = None


class Attachment(BaseModel):
    """Email attachment metadata."""
    index: int
    filename: str
    content_type: str
    size: Optional[int] = None


class Email(BaseModel):
    """Complete email with headers, body and attachments."""
    header: EmailHeader
    body: Optional[EmailBody] = None
    attachments: list[Attachment] = []


class MailboxStatus(BaseModel):
    """Mailbox status information."""
    name: str
    exists: int
    recent: int
    unseen: int
    uidnext: int
    uidvalidity: int


class MailboxInfo(BaseModel):
    """Mailbox folder information."""
    name: str
    delimiter: str
    flags: list[str] = []


class SearchResult(BaseModel):
    """Search result with UIDs."""
    uids: list[int]
    count: int


class CachedOverview(BaseModel):
    """Cached mailbox overview."""
    mailbox: str
    emails: list[EmailHeader]
    total: int
    unread: int
    last_updated: datetime


class AutoArchiveSender(BaseModel):
    """Auto-archive sender entry."""
    email: str
    comment: Optional[str] = None
    added_at: datetime
