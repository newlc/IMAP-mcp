"""IMAP MCP Server -- Model Context Protocol server for IMAP email operations.

Exposes IMAP capabilities (reading, searching, moving, caching, watching)
as MCP tools that can be consumed by any MCP-compatible client.
"""

import asyncio
import json
from typing import Any, Optional

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import (
    GetPromptResult,
    Prompt,
    PromptArgument,
    PromptMessage,
    Resource,
    ResourceTemplate,
    TextContent,
    Tool,
)

from .accounts import AccountManager


# Module-level state.
# ``account_manager`` holds every configured account; tool handlers route
# calls through it. ``_config_path`` is set from the CLI ``--config``
# argument so the (still-supported) ``auto_connect`` tool can find the
# config file on demand.
account_manager = AccountManager()
_config_path = "config.json"

# Write-mode flag (set from CLI --write argument). When False, the server runs
# read-only: tools that send mail, delete messages, mutate folders, or change
# server-side filters are not exposed and refuse to run if invoked anyway.
_write_enabled = False

# Names of tools that require --write mode.
WRITE_TOOL_NAMES = frozenset({
    "send_email",
    "reply_email",
    "forward_email",
    "delete_email",
    "rename_mailbox",
    "delete_mailbox",
    "empty_mailbox",
    "sieve_put_script",
    "sieve_delete_script",
    "sieve_activate_script",
    "rotate_encryption_key",
    "import_cache",
})

# Create MCP server
server = Server("imap-mcp")


# Reusable schema fragment for the per-tool ``account`` selector.
_ACCOUNT_PROP = {
    "type": "string",
    "description": (
        "Account name (matches accounts[].name in config.json). "
        "Omit to use the default account."
    ),
}


def _client(account: Optional[str]):
    """Return the wrapper for the requested account (lazily connecting)."""
    return account_manager.get(account)


# Backwards-compatibility shim: legacy tests reference ``srv.imap_client``
# expecting it to behave like a single ImapClientWrapper. Now that the server
# is multi-account, route attribute access to the default account's wrapper.
class _DefaultAccountProxy:
    def __getattr__(self, name):
        return getattr(account_manager.get(None), name)

    def __setattr__(self, name, value):
        setattr(account_manager.get(None), name, value)


imap_client = _DefaultAccountProxy()


def make_tool(
    name: str,
    description: str,
    properties: dict,
    required: Optional[list[str]] = None,
    multi_account: bool = True,
) -> Tool:
    """Helper to create a Tool definition with a JSON Schema input.

    Unless ``multi_account=False`` is passed (for global tools like
    ``auto_connect`` or ``list_accounts``), an optional ``account``
    parameter is automatically merged into every tool so callers can
    target any configured account.
    """
    if multi_account:
        properties = {**properties, "account": _ACCOUNT_PROP}
    return Tool(
        name=name,
        description=description,
        inputSchema={
            "type": "object",
            "properties": properties,
            "required": required or [],
        },
    )


@server.list_tools()
async def list_tools() -> list[Tool]:
    """List all available IMAP tools.

    Tools that send mail or delete messages (``send_email``, ``reply_email``,
    ``forward_email``, ``delete_email``) are only exposed when the server was
    started with ``--write``.
    """
    base_tools = [
        # === Connection / accounts ===
        make_tool(
            "auto_connect",
            "Load config.json (multi-account format) and start IDLE watchers "
            "for accounts with cache.enabled=true. Other accounts connect "
            "lazily on first use.",
            {},
            multi_account=False,
        ),
        make_tool(
            "list_accounts",
            "List configured accounts (name, default, IMAP host, cache settings, "
            "connection state).",
            {},
            multi_account=False,
        ),
        make_tool(
            "accounts_health",
            "Per-account reachability check (NOOP on the IMAP socket, cache "
            "status, watcher state). Does not open new connections -- only "
            "checks accounts that are already connected.",
            {},
            multi_account=False,
        ),
        make_tool(
            "disconnect",
            "Close the IMAP connection for one account, or all accounts when "
            "called without 'account'.",
            {
                "account": {**_ACCOUNT_PROP,
                            "description": "Account to disconnect; omit for all accounts."},
            },
            multi_account=False,
        ),
        # === Mailboxes ===
        make_tool(
            "list_mailboxes",
            "List all mailbox folders. Returns {mailboxes, total, next_cursor}; "
            "pass cursor + limit to paginate on servers with thousands of folders.",
            {
                "pattern": {"type": "string", "description": "Filter pattern (default: *)"},
                "cursor": {"type": "number", "description": "Pagination offset (default: 0)"},
                "limit": {"type": "number", "description": "Max mailboxes to return (omit for all)"},
            },
        ),
        make_tool(
            "select_mailbox",
            "Select/open a mailbox folder",
            {
                "mailbox": {"type": "string", "description": "Mailbox name (e.g., INBOX, Sent)"},
            },
            ["mailbox"],
        ),
        make_tool(
            "create_mailbox",
            "Create a new mailbox folder",
            {
                "mailbox": {"type": "string", "description": "New mailbox name"},
            },
            ["mailbox"],
        ),
        make_tool(
            "get_mailbox_status",
            "Get mailbox status (message count, unseen, etc.)",
            {
                "mailbox": {"type": "string", "description": "Mailbox name"},
            },
            ["mailbox"],
        ),
        # === Email Reading ===
        make_tool(
            "fetch_emails",
            "Fetch emails from mailbox with optional filters",
            {
                "mailbox": {"type": "string", "description": "Mailbox name (default: current)"},
                "limit": {"type": "number", "description": "Max emails to fetch (default: 20)"},
                "offset": {"type": "number", "description": "Skip first N emails (default: 0)"},
                "since": {"type": "string", "description": "Emails since date (ISO format)"},
                "before": {"type": "string", "description": "Emails before date (ISO format)"},
            },
        ),
        make_tool(
            "get_email",
            "Get complete email by UID. With peek_bytes set, fetches only "
            "headers + first N body bytes (no attachments, no cache write) -- "
            "useful for very large messages.",
            {
                "uid": {"type": "number", "description": "Email UID"},
                "mailbox": {"type": "string", "description": "Mailbox name (default: current)"},
                "peek_bytes": {"type": "number", "description": "If set, partial fetch limited to N body bytes (RFC 3501 BODY[TEXT]<0.N>)"},
            },
            ["uid"],
        ),
        make_tool(
            "get_email_headers",
            "Get only email headers (faster)",
            {
                "uid": {"type": "number", "description": "Email UID"},
                "mailbox": {"type": "string", "description": "Mailbox name (default: current)"},
            },
            ["uid"],
        ),
        make_tool(
            "get_email_body",
            "Get email body content",
            {
                "uid": {"type": "number", "description": "Email UID"},
                "mailbox": {"type": "string", "description": "Mailbox name (default: current)"},
                "format": {
                    "type": "string",
                    "enum": ["text", "html"],
                    "description": "Body format (default: text)",
                },
            },
            ["uid"],
        ),
        make_tool(
            "get_attachments",
            "List attachments of an email",
            {
                "uid": {"type": "number", "description": "Email UID"},
                "mailbox": {"type": "string", "description": "Mailbox name (default: current)"},
            },
            ["uid"],
        ),
        make_tool(
            "download_attachment",
            "Download attachment content (base64)",
            {
                "uid": {"type": "number", "description": "Email UID"},
                "attachmentIndex": {"type": "number", "description": "Attachment index (0-based)"},
                "mailbox": {"type": "string", "description": "Mailbox name (default: current)"},
            },
            ["uid", "attachmentIndex"],
        ),
        make_tool(
            "get_thread",
            "Get email thread/conversation",
            {
                "uid": {"type": "number", "description": "Email UID (any email in thread)"},
                "mailbox": {"type": "string", "description": "Mailbox name (default: current)"},
            },
            ["uid"],
        ),
        # === Search ===
        make_tool(
            "search_emails",
            "Search emails with query",
            {
                "query": {"type": "string", "description": "Search query (IMAP SEARCH syntax or text)"},
                "mailbox": {"type": "string", "description": "Mailbox name (default: current)"},
                "limit": {"type": "number", "description": "Max results (default: 50)"},
            },
            ["query"],
        ),
        make_tool(
            "search_by_sender",
            "Search emails by sender address",
            {
                "sender": {"type": "string", "description": "Sender email address or name"},
                "mailbox": {"type": "string", "description": "Mailbox name (default: current)"},
                "limit": {"type": "number", "description": "Max results (default: 50)"},
            },
            ["sender"],
        ),
        make_tool(
            "search_by_subject",
            "Search emails by subject",
            {
                "subject": {"type": "string", "description": "Subject text to search"},
                "mailbox": {"type": "string", "description": "Mailbox name (default: current)"},
                "limit": {"type": "number", "description": "Max results (default: 50)"},
            },
            ["subject"],
        ),
        make_tool(
            "search_by_date",
            "Search emails by date range",
            {
                "since": {"type": "string", "description": "Emails since date (ISO format)"},
                "before": {"type": "string", "description": "Emails before date (ISO format)"},
                "mailbox": {"type": "string", "description": "Mailbox name (default: current)"},
                "limit": {"type": "number", "description": "Max results (default: 50)"},
            },
        ),
        make_tool(
            "search_unread",
            "Get all unread emails",
            {
                "mailbox": {"type": "string", "description": "Mailbox name (default: current)"},
                "limit": {"type": "number", "description": "Max results (default: 50)"},
            },
        ),
        make_tool(
            "search_flagged",
            "Get all flagged/starred emails",
            {
                "mailbox": {"type": "string", "description": "Mailbox name (default: current)"},
                "limit": {"type": "number", "description": "Max results (default: 50)"},
            },
        ),
        # === Actions ===
        make_tool(
            "mark_read",
            "Mark emails as read",
            {
                "uids": {
                    "type": "array",
                    "items": {"type": "number"},
                    "description": "Email UIDs",
                },
                "mailbox": {"type": "string", "description": "Mailbox name (default: current)"},
            },
            ["uids"],
        ),
        make_tool(
            "mark_unread",
            "Mark emails as unread",
            {
                "uids": {
                    "type": "array",
                    "items": {"type": "number"},
                    "description": "Email UIDs",
                },
                "mailbox": {"type": "string", "description": "Mailbox name (default: current)"},
            },
            ["uids"],
        ),
        make_tool(
            "flag_email",
            "Add flag to emails",
            {
                "uids": {
                    "type": "array",
                    "items": {"type": "number"},
                    "description": "Email UIDs",
                },
                "flag": {"type": "string", "description": "Flag name (e.g., \\Flagged, \\Important)"},
                "mailbox": {"type": "string", "description": "Mailbox name (default: current)"},
            },
            ["uids", "flag"],
        ),
        make_tool(
            "unflag_email",
            "Remove flag from emails",
            {
                "uids": {
                    "type": "array",
                    "items": {"type": "number"},
                    "description": "Email UIDs",
                },
                "flag": {"type": "string", "description": "Flag name to remove"},
                "mailbox": {"type": "string", "description": "Mailbox name (default: current)"},
            },
            ["uids", "flag"],
        ),
        make_tool(
            "move_email",
            "Move emails to another mailbox",
            {
                "uids": {
                    "type": "array",
                    "items": {"type": "number"},
                    "description": "Email UIDs",
                },
                "destination": {"type": "string", "description": "Destination mailbox"},
                "mailbox": {"type": "string", "description": "Source mailbox (default: current)"},
            },
            ["uids", "destination"],
        ),
        make_tool(
            "copy_email",
            "Copy emails to another mailbox",
            {
                "uids": {
                    "type": "array",
                    "items": {"type": "number"},
                    "description": "Email UIDs",
                },
                "destination": {"type": "string", "description": "Destination mailbox"},
                "mailbox": {"type": "string", "description": "Source mailbox (default: current)"},
            },
            ["uids", "destination"],
        ),
        make_tool(
            "archive_email",
            "Archive emails (move to Archive folder)",
            {
                "uids": {
                    "type": "array",
                    "items": {"type": "number"},
                    "description": "Email UIDs",
                },
                "mailbox": {"type": "string", "description": "Source mailbox (default: current)"},
                "archiveFolder": {"type": "string", "description": "Archive folder name (default: Archive)"},
            },
            ["uids"],
        ),
        make_tool(
            "save_draft",
            "Save email as draft (automatically includes user signature from config)",
            {
                "to": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Recipient addresses",
                },
                "subject": {"type": "string", "description": "Email subject"},
                "body": {"type": "string", "description": "Email body (plain text)"},
                "cc": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "CC addresses",
                },
                "bcc": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "BCC addresses",
                },
                "htmlBody": {"type": "string", "description": "Email body (HTML, optional)"},
                "attachments": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Local file paths to attach (optional)",
                },
                "draftsFolder": {"type": "string", "description": "Drafts folder name (default: Drafts)"},
                "includeSignature": {"type": "boolean", "description": "Include signature from config (default: true)"},
                "idempotencyKey": {"type": "string", "description": "If set (and persistent cache enabled), repeated calls with the same key return the original result without re-appending."},
            },
            ["to", "subject", "body"],
        ),
        # === Statistics ===
        make_tool(
            "get_unread_count",
            "Get count of unread emails",
            {
                "mailbox": {"type": "string", "description": "Mailbox name (default: INBOX)"},
            },
        ),
        make_tool(
            "get_total_count",
            "Get total email count in mailbox",
            {
                "mailbox": {"type": "string", "description": "Mailbox name (default: INBOX)"},
            },
        ),
        # === Cache & Watch ===
        make_tool(
            "get_cached_overview",
            "Get cached email overview for INBOX, next, waiting, someday (from in-memory cache)",
            {
                "mailbox": {
                    "type": "string",
                    "enum": ["inbox", "next", "waiting", "someday"],
                    "description": "Specific mailbox to get (inbox, next, waiting, someday) or omit for all",
                },
                "limit": {"type": "number", "description": "Max emails per mailbox (default: 20)"},
            },
        ),
        make_tool(
            "refresh_cache",
            "Force refresh of email cache for all watched mailboxes",
            {},
        ),
        make_tool(
            "start_watch",
            "Start permanent IDLE watch on INBOX, next, waiting, someday",
            {},
        ),
        make_tool(
            "stop_watch",
            "Stop the permanent IDLE watch",
            {},
        ),
        make_tool(
            "idle_watch",
            "Start watching mailbox for new emails (IMAP IDLE) - single mailbox, temporary",
            {
                "mailbox": {"type": "string", "description": "Mailbox to watch (default: INBOX)"},
                "timeout": {"type": "number", "description": "Watch timeout in seconds (default: 300)"},
            },
        ),
        # === Sync & Persistent Cache ===
        make_tool(
            "sync_emails",
            "Download emails for a date range into persistent SQLite cache (with bodies and attachments). Incremental by default — only downloads new emails on subsequent runs.",
            {
                "mailbox": {"type": "string", "description": "Mailbox to sync (default: INBOX)"},
                "since": {"type": "string", "description": "Sync emails since date (e.g. 2026-01-25)"},
                "before": {"type": "string", "description": "Sync emails before date (e.g. 2026-04-26)"},
                "full": {"type": "boolean", "description": "Force full re-sync, ignoring cache (default: false)"},
            },
        ),
        make_tool(
            "load_cache",
            "Flexible cache loader — download emails (with bodies and attachments) into local SQLite for offline analysis. "
            "Modes: 'recent' (last N emails), 'new' (only emails newer than cached), "
            "'older' (N emails older than oldest cached — go further back in time), "
            "'range' (emails between since/before dates).",
            {
                "mailbox": {"type": "string", "description": "Mailbox to load (default: INBOX)"},
                "mode": {
                    "type": "string",
                    "enum": ["recent", "new", "older", "range"],
                    "description": "Loading mode (default: recent)",
                },
                "count": {"type": "number", "description": "Number of emails to load (for recent/older modes, default: 100)"},
                "since": {"type": "string", "description": "Start date for range mode (e.g. 2026-01-25)"},
                "before": {"type": "string", "description": "End date for range mode (e.g. 2026-04-26)"},
                "include_attachments": {"type": "boolean", "description": "Download attachments too (default: true)"},
            },
        ),
        make_tool(
            "get_cache_stats",
            "Get persistent cache statistics (emails cached, attachments, database size)",
            {},
        ),
        make_tool(
            "cleanup_sent_log",
            "Delete sent_log rows older than N days. Returns counts.",
            {
                "older_than_days": {
                    "type": "number",
                    "description": "Cutoff in days (default: 30; 0 deletes everything)",
                },
            },
        ),
        make_tool(
            "audit_log_query",
            "Read recent audit-log entries (every tool call is logged with "
            "account, args, status). Newest first.",
            {
                "limit": {"type": "number", "description": "Max rows (default: 100)"},
                "tool": {"type": "string", "description": "Filter by tool name"},
                "write_only": {"type": "boolean", "description": "Only --write tool calls"},
                "since": {"type": "string", "description": "ISO timestamp (inclusive)"},
            },
        ),
        make_tool(
            "cleanup_audit_log",
            "Delete audit_log rows older than N days. Returns counts.",
            {
                "older_than_days": {
                    "type": "number",
                    "description": "Cutoff in days (default: 90)",
                },
            },
        ),
        make_tool(
            "get_email_auth_results",
            "Parse SPF/DKIM/DMARC verdicts from Authentication-Results "
            "headers (RFC 8601). Cheap -- fetches only the relevant header.",
            {
                "uid": {"type": "number", "description": "Email UID"},
                "mailbox": {"type": "string", "description": "Mailbox (default: current)"},
            },
            ["uid"],
        ),
        make_tool(
            "vacuum_cache",
            "Compact the cache database (VACUUM + FTS5 optimize) and report "
            "size before/after.",
            {},
        ),
        make_tool(
            "export_cache",
            "Write a passphrase-protected, machine-portable snapshot of "
            "this account's cache (subject/body/attachments/sent_log/FTS5).",
            {
                "passphrase": {"type": "string", "description": "Passphrase to derive the export key (PBKDF2-HMAC-SHA256, 600k iters)"},
                "output_path": {"type": "string", "description": "Destination file path"},
            },
            ["passphrase", "output_path"],
        ),
        make_tool(
            "import_cache",
            "Replace this account's cache with the contents of a portable "
            "export. The current cache is wiped first; rejects on wrong "
            "passphrase or corrupted file.",
            {
                "passphrase": {"type": "string", "description": "Passphrase used at export time"},
                "input_path": {"type": "string", "description": "Path to the IMAPMCP1 file"},
            },
            ["passphrase", "input_path"],
        ),
        # === Auto-Archive ===
        make_tool(
            "get_auto_archive_list",
            "Get list of senders that are auto-archived",
            {},
        ),
        make_tool(
            "add_auto_archive_sender",
            "Add sender to auto-archive list",
            {
                "email": {"type": "string", "description": "Email address or domain to auto-archive"},
                "comment": {"type": "string", "description": "Optional comment/reason"},
            },
            ["email"],
        ),
        make_tool(
            "remove_auto_archive_sender",
            "Remove sender from auto-archive list",
            {
                "email": {"type": "string", "description": "Email address to remove"},
            },
            ["email"],
        ),
        make_tool(
            "reload_auto_archive",
            "Reload auto-archive config from file (after manual edit)",
            {},
        ),
        make_tool(
            "process_auto_archive",
            "Process INBOX and archive emails from listed senders. Use dry_run=true to preview without moving.",
            {
                "dry_run": {"type": "boolean", "description": "If true, only report what would be archived without moving (default: false)"},
            },
        ),
        # === Folder management (read-only-friendly) ===
        make_tool(
            "subscribe_mailbox",
            "Add a mailbox to the subscribed list (LSUB).",
            {"mailbox": {"type": "string", "description": "Mailbox name"}},
            ["mailbox"],
        ),
        make_tool(
            "unsubscribe_mailbox",
            "Remove a mailbox from the subscribed list.",
            {"mailbox": {"type": "string", "description": "Mailbox name"}},
            ["mailbox"],
        ),
        make_tool(
            "list_subscribed_mailboxes",
            "List subscribed mailboxes (LSUB).",
            {"pattern": {"type": "string", "description": "Filter pattern (default: *)"}},
        ),
        # === Draft management ===
        make_tool(
            "update_draft",
            "Replace an existing draft. APPENDs the new draft and EXPUNGEs the old "
            "UID from the Drafts folder. Returns the new UID when supported.",
            {
                "uid": {"type": "number", "description": "UID of the draft to replace"},
                "to": {
                    "type": "array", "items": {"type": "string"},
                    "description": "Recipient addresses",
                },
                "subject": {"type": "string", "description": "Draft subject"},
                "body": {"type": "string", "description": "Draft body (plain text)"},
                "cc": {"type": "array", "items": {"type": "string"}, "description": "CC addresses"},
                "bcc": {"type": "array", "items": {"type": "string"}, "description": "BCC addresses"},
                "htmlBody": {"type": "string", "description": "Draft body (HTML)"},
                "attachments": {
                    "type": "array", "items": {"type": "string"},
                    "description": "Local file paths to attach",
                },
                "draftsFolder": {"type": "string", "description": "Drafts folder (default: Drafts)"},
                "includeSignature": {"type": "boolean", "description": "Include signature (default: true)"},
            },
            ["uid", "to", "subject", "body"],
        ),
        make_tool(
            "delete_draft",
            "Permanently delete one draft (\\Deleted + EXPUNGE in the Drafts folder).",
            {
                "uid": {"type": "number", "description": "UID of the draft to delete"},
                "draftsFolder": {"type": "string", "description": "Drafts folder (default: Drafts)"},
            },
            ["uid"],
        ),
        # === Spam ===
        make_tool(
            "report_spam",
            "Move messages to the Spam folder and add the junk flag (default: $Junk). "
            "Trains server-side filters that respect IMAP keywords.",
            {
                "uids": {
                    "type": "array", "items": {"type": "number"},
                    "description": "Email UIDs",
                },
                "mailbox": {"type": "string", "description": "Source mailbox (default: current)"},
                "spamFolder": {"type": "string", "description": "Spam folder (default: from folders.spam or 'Spam')"},
                "flag": {"type": "string", "description": "Junk flag to set (default: $Junk; pass empty string to skip)"},
            },
            ["uids"],
        ),
        make_tool(
            "mark_not_spam",
            "Move messages out of the Spam folder back to INBOX (or destination), "
            "remove the junk flag and add $NotJunk where supported.",
            {
                "uids": {
                    "type": "array", "items": {"type": "number"},
                    "description": "Email UIDs",
                },
                "mailbox": {"type": "string", "description": "Source mailbox (default: from folders.spam or 'Spam')"},
                "destination": {"type": "string", "description": "Destination folder (default: from folders.inbox or 'INBOX')"},
            },
            ["uids"],
        ),
        # === HTML safety / inline images / calendar ===
        make_tool(
            "get_email_body_safe",
            "Return a sanitized HTML body (script/style/iframe stripped, "
            "javascript: URLs removed, style attribute filtered) plus the "
            "plain-text body and any inline cid: images. With "
            "inline_cid_images=true the HTML's cid: refs are replaced by "
            "data: URIs so the snippet renders standalone.",
            {
                "uid": {"type": "number", "description": "Email UID"},
                "mailbox": {"type": "string", "description": "Mailbox (default: current)"},
                "strip_remote_images": {"type": "boolean", "description": "Drop <img> tags whose src is not cid: (blocks tracking pixels)"},
                "strip_links": {"type": "boolean", "description": "Replace every href with #"},
                "inline_cid_images": {"type": "boolean", "description": "Replace cid: refs with data: URIs (default: true)"},
            },
            ["uid"],
        ),
        make_tool(
            "get_calendar_invites",
            "Parse text/calendar parts in an email and return one entry per "
            "VEVENT (method, uid, summary, start, end, organizer, attendees, "
            "...). Returns [] if there is no calendar part.",
            {
                "uid": {"type": "number", "description": "Email UID"},
                "mailbox": {"type": "string", "description": "Mailbox (default: current)"},
            },
            ["uid"],
        ),
        make_tool(
            "extract_recipients_from_thread",
            "Collect every distinct address that appears in the thread with "
            "the given seed UID. Useful for assembling reply-all lists "
            "without manually parsing headers.",
            {
                "uid": {"type": "number", "description": "Any UID in the thread"},
                "mailbox": {"type": "string", "description": "Mailbox (default: current)"},
            },
            ["uid"],
        ),
        make_tool(
            "thread_summary",
            "Compact LLM-friendly summary of the whole thread (title, span, "
            "participants, chronological message list).",
            {
                "uid": {"type": "number", "description": "Any UID in the thread"},
                "mailbox": {"type": "string", "description": "Mailbox (default: current)"},
            },
            ["uid"],
        ),
        make_tool(
            "extract_action_items",
            "Heuristic extraction of requests/questions/deadlines/blockers "
            "from one email's plain-text body. Pure regex -- no LLM call. "
            "Returns {requests, questions, deadlines, blockers}, each a "
            "list of {text, offset}.",
            {
                "uid": {"type": "number", "description": "Email UID"},
                "mailbox": {"type": "string", "description": "Mailbox (default: current)"},
            },
            ["uid"],
        ),
        make_tool(
            "watch_until",
            "Wait via IMAP IDLE for a new email matching from_addr/subject/"
            "unread. Returns the email summary on match or {'timed_out': "
            "true} after timeout seconds. Useful for OTP-style flows.",
            {
                "from_addr": {"type": "string", "description": "Filter by sender substring"},
                "subject": {"type": "string", "description": "Filter by subject substring"},
                "unread": {"type": "boolean", "description": "Only unread (default: true)"},
                "mailbox": {"type": "string", "description": "Mailbox to watch (default: INBOX)"},
                "timeout": {"type": "number", "description": "Max seconds to wait (default: 60)"},
            },
        ),
        # === Compact summaries ===
        make_tool(
            "get_email_summary",
            "Return a compact AI-friendly summary list (subject, sender, "
            "date, flags, snippet, has_attachments) for the given UIDs in a "
            "single round trip. Bodies are served from cache when present.",
            {
                "uids": {
                    "type": "array", "items": {"type": "number"},
                    "description": "Email UIDs to summarize",
                },
                "mailbox": {"type": "string", "description": "Mailbox (default: current)"},
                "body_chars": {"type": "number", "description": "Max plain-text snippet length (default: 300)"},
                "peek_bytes": {"type": "number", "description": "Bytes of message text to fetch per uncached UID via partial FETCH (default: max(body_chars*4, 1024); pass 0 for headers only)"},
            },
            ["uids"],
        ),
        # === Bulk operations by query ===
        make_tool(
            "bulk_action",
            "Apply one action to every message matching the search criteria. "
            "Supported actions: mark_read, mark_unread, flag, unflag, archive, "
            "delete, move, copy, report_spam. Use dry_run=true to preview.",
            {
                "action": {
                    "type": "string",
                    "enum": ["mark_read", "mark_unread", "flag", "unflag",
                             "archive", "delete", "move", "copy", "report_spam"],
                    "description": "Action to apply",
                },
                "mailbox": {"type": "string", "description": "Source mailbox (default: current)"},
                "from_addr": {"type": "string", "description": "Filter by sender"},
                "subject": {"type": "string", "description": "Filter by subject substring"},
                "since": {"type": "string", "description": "ISO date (inclusive)"},
                "before": {"type": "string", "description": "ISO date (exclusive)"},
                "unread": {"type": "boolean", "description": "Only unread / read"},
                "flagged": {"type": "boolean", "description": "Only flagged / unflagged"},
                "destination": {"type": "string", "description": "Required for move/copy; overrides default folder for archive/delete/report_spam"},
                "flag_name": {"type": "string", "description": "Used by flag/unflag (default: \\Flagged)"},
                "permanent": {"type": "boolean", "description": "For delete: \\Deleted+EXPUNGE instead of moving to Trash"},
                "dry_run": {"type": "boolean", "description": "Match without mutating (default: false)"},
                "limit": {"type": "number", "description": "Cap the number of UIDs to act on (oldest-first). The full match count is still reported."},
                "batch_size": {"type": "number", "description": "Max UIDs per IMAP command (default: 1000) -- some servers reject very long UID lists."},
            },
            ["action"],
        ),
        # === Search (advanced + FTS) ===
        make_tool(
            "search_advanced",
            "Combined IMAP SEARCH or local FTS5 query with multiple criteria "
            "(query, from, to, subject, date range, has_attachments, unread, flagged).",
            {
                "query": {"type": "string", "description": "Free-text query (TEXT for IMAP, MATCH for FTS)"},
                "from_addr": {"type": "string", "description": "Filter by sender"},
                "to_addr": {"type": "string", "description": "Filter by recipient"},
                "subject": {"type": "string", "description": "Filter by subject substring"},
                "since": {"type": "string", "description": "ISO date (inclusive)"},
                "before": {"type": "string", "description": "ISO date (exclusive)"},
                "has_attachments": {"type": "boolean", "description": "Only with/without attachments"},
                "unread": {"type": "boolean", "description": "Only unread (true) / read (false) / either (omit)"},
                "flagged": {"type": "boolean", "description": "Only flagged / unflagged"},
                "mailbox": {"type": "string", "description": "Mailbox to search (default: current)"},
                "use_fts": {"type": "boolean", "description": "Use local FTS5 over cached bodies (requires load_cache; default: false)"},
                "limit": {"type": "number", "description": "Max results (default: 50)"},
            },
        ),
        make_tool(
            "search_emails_fts",
            "Full-text search over the local FTS5 index. Requires that the cache "
            "has been populated via load_cache.",
            {
                "query": {"type": "string", "description": "FTS5 MATCH expression (e.g. 'invoice 2026', 'subject:report')"},
                "mailbox": {"type": "string", "description": "Restrict to one mailbox (optional)"},
                "limit": {"type": "number", "description": "Max results (default: 50)"},
            },
            ["query"],
        ),
        make_tool(
            "rebuild_search_index",
            "Rebuild the FTS5 index from cached email bodies.",
            {
                "mailbox": {"type": "string", "description": "Restrict to one mailbox (default: all)"},
            },
        ),
        # === Server metadata ===
        make_tool(
            "get_capabilities",
            "Return the IMAP server's CAPABILITY list (e.g. IDLE, THREAD=REFERENCES, QUOTA).",
            {},
        ),
        make_tool(
            "get_namespace",
            "Return the IMAP NAMESPACE (personal/other/shared) for this server.",
            {},
        ),
        make_tool(
            "get_quota",
            "Return IMAP QUOTA usage for a mailbox (default: INBOX).",
            {"mailbox": {"type": "string", "description": "Mailbox name (default: INBOX)"}},
        ),
        make_tool(
            "get_server_id",
            "Return server-side ID info via IMAP ID (RFC 2971), if supported.",
            {},
        ),
        # === Sieve (server-side rules) -- read-only ===
        make_tool(
            "sieve_list_scripts",
            "List Sieve scripts on the ManageSieve server (RFC 5804). "
            "Requires sieve.host configured for the account.",
            {},
        ),
        make_tool(
            "sieve_get_script",
            "Fetch the source of a Sieve script by name.",
            {"name": {"type": "string", "description": "Script name"}},
            ["name"],
        ),
        make_tool(
            "sieve_check_script",
            "Validate a Sieve script's syntax against the server (CHECKSCRIPT).",
            {"content": {"type": "string", "description": "Sieve script source"}},
            ["content"],
        ),
    ]

    # === Write-mode tools (only exposed when --write is set) ===
    write_tools = [
        make_tool(
            "send_email",
            "Send an email via SMTP. Saves a copy to the Sent folder by default. "
            "Requires --write mode and SMTP configured in config.json.",
            {
                "to": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Recipient addresses",
                },
                "subject": {"type": "string", "description": "Email subject"},
                "body": {"type": "string", "description": "Email body (plain text)"},
                "cc": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "CC addresses",
                },
                "bcc": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "BCC addresses (not visible in headers)",
                },
                "htmlBody": {"type": "string", "description": "Email body (HTML, optional)"},
                "attachments": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Local file paths to attach (optional)",
                },
                "includeSignature": {"type": "boolean", "description": "Include signature from config (default: true)"},
                "saveToSent": {"type": "boolean", "description": "Save copy to Sent folder (default: true)"},
                "sentFolder": {"type": "string", "description": "Sent folder name (default: from folders.sent or 'Sent')"},
                "idempotencyKey": {"type": "string", "description": "If supplied (and persistent cache enabled), repeated calls with the same key return the original result without re-sending."},
            },
            ["to", "subject", "body"],
        ),
        make_tool(
            "reply_email",
            "Reply to an email by UID. With reply_all=true, includes original To/Cc "
            "recipients (minus the user's own address) in Cc. Requires --write mode.",
            {
                "uid": {"type": "number", "description": "UID of the email to reply to"},
                "body": {"type": "string", "description": "Reply body (plain text)"},
                "mailbox": {"type": "string", "description": "Mailbox containing the email (default: current)"},
                "htmlBody": {"type": "string", "description": "Reply body (HTML, optional)"},
                "replyAll": {"type": "boolean", "description": "Reply to all original recipients (default: false)"},
                "attachments": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Local file paths to attach (optional)",
                },
                "includeSignature": {"type": "boolean", "description": "Include signature (default: true)"},
                "quoteOriginal": {"type": "boolean", "description": "Append quoted original message (default: true)"},
                "saveToSent": {"type": "boolean", "description": "Save copy to Sent folder (default: true)"},
                "idempotencyKey": {"type": "string", "description": "Idempotency key (see send_email)"},
            },
            ["uid", "body"],
        ),
        make_tool(
            "forward_email",
            "Forward an email by UID to new recipients, preserving original "
            "attachments by default. Requires --write mode.",
            {
                "uid": {"type": "number", "description": "UID of the email to forward"},
                "to": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Recipient addresses",
                },
                "body": {"type": "string", "description": "Optional intro text added before the forwarded content"},
                "mailbox": {"type": "string", "description": "Mailbox containing the email (default: current)"},
                "cc": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "CC addresses",
                },
                "bcc": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "BCC addresses",
                },
                "htmlBody": {"type": "string", "description": "Optional HTML intro"},
                "includeAttachments": {"type": "boolean", "description": "Re-attach original attachments (default: true)"},
                "extraAttachments": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Additional local files to attach (optional)",
                },
                "includeSignature": {"type": "boolean", "description": "Include signature (default: true)"},
                "saveToSent": {"type": "boolean", "description": "Save copy to Sent folder (default: true)"},
                "idempotencyKey": {"type": "string", "description": "Idempotency key (see send_email)"},
            },
            ["uid", "to"],
        ),
        make_tool(
            "delete_email",
            "Delete emails. Default: move to the Trash folder. With permanent=true: "
            "set the \\Deleted flag and EXPUNGE without going through Trash. "
            "Requires --write mode.",
            {
                "uids": {
                    "type": "array",
                    "items": {"type": "number"},
                    "description": "Email UIDs",
                },
                "mailbox": {"type": "string", "description": "Source mailbox (default: current)"},
                "permanent": {"type": "boolean", "description": "Permanent delete (\\Deleted + EXPUNGE) instead of moving to Trash (default: false)"},
                "trashFolder": {"type": "string", "description": "Trash folder name (default: from folders.trash or 'Trash')"},
            },
            ["uids"],
        ),
        # === Folder management (destructive) ===
        make_tool(
            "rename_mailbox",
            "Rename a mailbox folder. Requires --write mode.",
            {
                "old_name": {"type": "string", "description": "Existing mailbox name"},
                "new_name": {"type": "string", "description": "New mailbox name"},
            },
            ["old_name", "new_name"],
        ),
        make_tool(
            "delete_mailbox",
            "Delete a mailbox folder. Some servers refuse to delete non-empty "
            "folders. Requires --write mode.",
            {"mailbox": {"type": "string", "description": "Mailbox name"}},
            ["mailbox"],
        ),
        make_tool(
            "empty_mailbox",
            "Delete every message in a mailbox (\\Deleted + EXPUNGE) but keep the "
            "folder itself. Requires --write mode.",
            {"mailbox": {"type": "string", "description": "Mailbox name"}},
            ["mailbox"],
        ),
        # === Sieve (server-side rules) -- write ===
        make_tool(
            "sieve_put_script",
            "Upload (create or replace) a Sieve script. Requires --write mode.",
            {
                "name": {"type": "string", "description": "Script name"},
                "content": {"type": "string", "description": "Sieve script source"},
            },
            ["name", "content"],
        ),
        make_tool(
            "sieve_delete_script",
            "Delete a Sieve script by name. Requires --write mode.",
            {"name": {"type": "string", "description": "Script name"}},
            ["name"],
        ),
        make_tool(
            "sieve_activate_script",
            "Activate a Sieve script (or pass empty name to deactivate all). "
            "Requires --write mode.",
            {"name": {"type": "string", "description": "Script name (empty = none active)"}},
            ["name"],
        ),
        make_tool(
            "rotate_encryption_key",
            "Generate a fresh Fernet key for the encrypted cache and re-encrypt "
            "the on-disk snapshot with it. The previous key is backed up to "
            "OS keyring under '<keyring_username>.previous'. Requires --write.",
            {},
        ),
    ]

    if _write_enabled:
        return base_tools + write_tools
    return base_tools


def serialize_result(result: Any) -> str:
    """Serialize result to JSON string."""
    if hasattr(result, "model_dump"):
        return json.dumps(result.model_dump(), default=str, ensure_ascii=False)
    elif isinstance(result, list):
        items = [
            item.model_dump() if hasattr(item, "model_dump") else item
            for item in result
        ]
        return json.dumps(items, default=str, ensure_ascii=False)
    elif isinstance(result, dict):
        return json.dumps(result, default=str, ensure_ascii=False)
    elif isinstance(result, (bool, int, float, str)):
        return json.dumps({"result": result}, ensure_ascii=False)
    else:
        return json.dumps({"result": str(result)}, ensure_ascii=False)


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    """Handle tool calls."""
    try:
        result = await handle_tool_call(name, arguments)
        # Opportunistically flush any pending resource-update notifications
        # picked up by IDLE watchers since the last call.
        try:
            await _flush_resource_notifications()
        except Exception:
            pass
        return [TextContent(type="text", text=serialize_result(result))]
    except Exception as e:
        return [TextContent(type="text", text=json.dumps({"error": str(e)}))]


async def handle_tool_call(name: str, args: dict) -> Any:
    """Route tool calls to appropriate handler."""

    if name in WRITE_TOOL_NAMES and not _write_enabled:
        raise PermissionError(
            f"Tool '{name}' is disabled in read-only mode. "
            "Restart imap-mcp with --write to enable destructive operations."
        )

    # bulk_action covers many actions; gate the destructive ones individually.
    if name == "bulk_action" and not _write_enabled:
        if args.get("action") == "delete":
            raise PermissionError(
                "bulk_action(action='delete') is disabled in read-only mode. "
                "Restart imap-mcp with --write."
            )

    account = args.get("account")
    is_write = (
        name in WRITE_TOOL_NAMES
        or (name == "bulk_action" and args.get("action") in
            {"delete", "move", "copy", "archive", "report_spam",
             "mark_read", "mark_unread", "flag", "unflag"})
    )

    try:
        result = await _dispatch_tool(name, args, account)
        _record_audit(account, name, is_write, args, "ok", None)
        return result
    except Exception as exc:
        _record_audit(account, name, is_write, args, "error", str(exc))
        raise


def _record_audit(
    account: Optional[str], name: str, is_write: bool,
    args: dict, status: str, error: Optional[str],
) -> None:
    """Best-effort: write to the default account's cache audit_log table."""
    try:
        if not account_manager.has_accounts():
            return
        # Pick a cache to write to: prefer the named account, fall back to
        # the default. Skip silently if neither has a cache.
        try:
            target = account_manager.get_account(account)
        except (ValueError, RuntimeError):
            target = None
        if target is None:
            return
        if not target._connected:
            # Don't lazily-connect just to log -- that would defeat lazy mode.
            return
        cache = target.client.email_cache
        if not cache:
            return
        # Strip the account arg from the snapshot since it's already a
        # column. Drop bulky args (file contents, full bodies).
        log_args = {k: v for k, v in args.items() if k != "account"}
        for k in ("body", "htmlBody", "content"):
            if k in log_args and isinstance(log_args[k], str) and len(log_args[k]) > 200:
                log_args[k] = log_args[k][:200] + "..."
        cache.record_audit(
            account or account_manager.default_name,
            name, is_write, log_args, status, error,
        )
    except Exception:
        # Audit logging must never break the actual call.
        pass


async def _dispatch_tool(name: str, args: dict, account: Optional[str]) -> Any:
    """Real tool dispatch -- the original handle_tool_call body."""

    # ------------------------------------------------------------------
    # Global tools that don't target a specific account
    # ------------------------------------------------------------------
    if name == "auto_connect":
        account_manager.load_config(_config_path)
        _wire_resource_subscriptions()
        return {
            "loaded": True,
            "config_path": _config_path,
            "default_account": account_manager.default_name,
            "accounts": account_manager.list_accounts(),
        }
    elif name == "list_accounts":
        return account_manager.list_accounts()
    elif name == "accounts_health":
        out = []
        for acct in account_manager.accounts.values():
            try:
                health = acct.client.health_check() if acct._connected else {
                    "connected": False, "ok": False, "reason": "not connected (lazy)",
                }
            except Exception as exc:
                health = {"connected": False, "ok": False, "reason": str(exc)}
            out.append({"name": acct.name, **health})
        return out
    elif name == "disconnect":
        if account:
            account_manager.get_account(account).disconnect()
            return {"disconnected": account}
        account_manager.disconnect_all()
        return {"disconnected": "all"}

    # Anything below requires an initialized account manager.
    if not account_manager.has_accounts():
        raise RuntimeError(
            "No accounts loaded. Call auto_connect first."
        )
    cli = _client(account)

    # ------------------------------------------------------------------
    # Mailboxes
    # ------------------------------------------------------------------
    if name == "list_mailboxes":
        return cli.list_mailboxes(
            pattern=args.get("pattern", "*"),
            cursor=int(args.get("cursor", 0)),
            limit=args.get("limit"),
        )
    elif name == "select_mailbox":
        return cli.select_mailbox(args["mailbox"])
    elif name == "create_mailbox":
        return cli.create_mailbox(args["mailbox"])
    elif name == "get_mailbox_status":
        return cli.get_mailbox_status(args["mailbox"])
    elif name == "rename_mailbox":
        return cli.rename_mailbox(args["old_name"], args["new_name"])
    elif name == "delete_mailbox":
        return cli.delete_mailbox(args["mailbox"])
    elif name == "empty_mailbox":
        return cli.empty_mailbox(args["mailbox"])
    elif name == "subscribe_mailbox":
        return cli.subscribe_mailbox(args["mailbox"])
    elif name == "unsubscribe_mailbox":
        return cli.unsubscribe_mailbox(args["mailbox"])
    elif name == "list_subscribed_mailboxes":
        return cli.list_subscribed_mailboxes(pattern=args.get("pattern", "*"))

    # ------------------------------------------------------------------
    # Email reading
    # ------------------------------------------------------------------
    elif name == "fetch_emails":
        return cli.fetch_emails(
            mailbox=args.get("mailbox"),
            limit=args.get("limit", 20),
            offset=args.get("offset", 0),
            since=args.get("since"),
            before=args.get("before"),
        )
    elif name == "get_email":
        return cli.get_email(
            uid=args["uid"], mailbox=args.get("mailbox"),
            peek_bytes=args.get("peek_bytes"),
        )
    elif name == "get_email_headers":
        return cli.get_email_headers(uid=args["uid"], mailbox=args.get("mailbox"))
    elif name == "get_email_body":
        return cli.get_email_body(
            uid=args["uid"],
            mailbox=args.get("mailbox"),
            format=args.get("format", "text"),
        )
    elif name == "get_attachments":
        return cli.get_attachments(uid=args["uid"], mailbox=args.get("mailbox"))
    elif name == "download_attachment":
        filename, content_type, data = cli.download_attachment(
            uid=args["uid"],
            attachment_index=args["attachmentIndex"],
            mailbox=args.get("mailbox"),
        )
        return {
            "filename": filename,
            "contentType": content_type,
            "data": data.decode("ascii"),
        }
    elif name == "get_thread":
        return cli.get_thread(uid=args["uid"], mailbox=args.get("mailbox"))
    elif name == "get_email_body_safe":
        return cli.get_email_body_safe(
            uid=args["uid"],
            mailbox=args.get("mailbox"),
            strip_remote_images=args.get("strip_remote_images", False),
            strip_links=args.get("strip_links", False),
            inline_cid_images=args.get("inline_cid_images", True),
        )
    elif name == "get_calendar_invites":
        return cli.get_calendar_invites(
            uid=args["uid"], mailbox=args.get("mailbox")
        )
    elif name == "extract_recipients_from_thread":
        return cli.extract_recipients_from_thread(
            uid=args["uid"], mailbox=args.get("mailbox")
        )
    elif name == "thread_summary":
        return cli.thread_summary(
            uid=args["uid"], mailbox=args.get("mailbox")
        )
    elif name == "extract_action_items":
        return cli.extract_action_items(
            uid=args["uid"], mailbox=args.get("mailbox")
        )
    elif name == "watch_until":
        criteria = {
            k: args[k] for k in ("from_addr", "subject", "unread")
            if k in args
        }
        return cli.watch_until(
            criteria=criteria,
            mailbox=args.get("mailbox", "INBOX"),
            timeout=int(args.get("timeout", 60)),
        )
    elif name == "get_email_summary":
        return cli.get_email_summary(
            uids=args["uids"],
            mailbox=args.get("mailbox"),
            body_chars=args.get("body_chars", 300),
            peek_bytes=args.get("peek_bytes"),
        )
    elif name == "bulk_action":
        return cli.bulk_action(
            action=args["action"],
            mailbox=args.get("mailbox"),
            from_addr=args.get("from_addr"),
            subject=args.get("subject"),
            since=args.get("since"),
            before=args.get("before"),
            unread=args.get("unread"),
            flagged=args.get("flagged"),
            destination=args.get("destination"),
            flag_name=args.get("flag_name"),
            permanent=args.get("permanent", False),
            dry_run=args.get("dry_run", False),
            limit=args.get("limit"),
            batch_size=args.get("batch_size", 1000),
        )

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------
    elif name == "search_emails":
        return cli.search_emails(
            query=args["query"],
            mailbox=args.get("mailbox"),
            limit=args.get("limit", 50),
        )
    elif name == "search_by_sender":
        return cli.search_by_sender(
            sender=args["sender"],
            mailbox=args.get("mailbox"),
            limit=args.get("limit", 50),
        )
    elif name == "search_by_subject":
        return cli.search_by_subject(
            subject=args["subject"],
            mailbox=args.get("mailbox"),
            limit=args.get("limit", 50),
        )
    elif name == "search_by_date":
        return cli.search_by_date(
            mailbox=args.get("mailbox"),
            since=args.get("since"),
            before=args.get("before"),
            limit=args.get("limit", 50),
        )
    elif name == "search_unread":
        return cli.search_unread(
            mailbox=args.get("mailbox"),
            limit=args.get("limit", 50),
        )
    elif name == "search_flagged":
        return cli.search_flagged(
            mailbox=args.get("mailbox"),
            limit=args.get("limit", 50),
        )
    elif name == "search_advanced":
        return cli.search_advanced(
            query=args.get("query"),
            from_addr=args.get("from_addr"),
            to_addr=args.get("to_addr"),
            subject=args.get("subject"),
            since=args.get("since"),
            before=args.get("before"),
            has_attachments=args.get("has_attachments"),
            unread=args.get("unread"),
            flagged=args.get("flagged"),
            mailbox=args.get("mailbox"),
            use_fts=args.get("use_fts", False),
            limit=args.get("limit", 50),
        )
    elif name == "search_emails_fts":
        return cli.search_emails_fts(
            query=args["query"],
            mailbox=args.get("mailbox"),
            limit=args.get("limit", 50),
        )
    elif name == "rebuild_search_index":
        return cli.rebuild_search_index(mailbox=args.get("mailbox"))

    # ------------------------------------------------------------------
    # Actions (read-only-friendly)
    # ------------------------------------------------------------------
    elif name == "mark_read":
        return cli.mark_read(uids=args["uids"], mailbox=args.get("mailbox"))
    elif name == "mark_unread":
        return cli.mark_unread(uids=args["uids"], mailbox=args.get("mailbox"))
    elif name == "flag_email":
        return cli.flag_email(
            uids=args["uids"], flag=args["flag"], mailbox=args.get("mailbox")
        )
    elif name == "unflag_email":
        return cli.unflag_email(
            uids=args["uids"], flag=args["flag"], mailbox=args.get("mailbox")
        )
    elif name == "move_email":
        return cli.move_email(
            uids=args["uids"],
            destination=args["destination"],
            mailbox=args.get("mailbox"),
        )
    elif name == "copy_email":
        return cli.copy_email(
            uids=args["uids"],
            destination=args["destination"],
            mailbox=args.get("mailbox"),
        )
    elif name == "archive_email":
        return cli.archive_email(
            uids=args["uids"],
            mailbox=args.get("mailbox"),
            archive_folder=args.get("archiveFolder", "Archive"),
        )
    elif name == "save_draft":
        return cli.save_draft(
            to=args["to"],
            subject=args["subject"],
            body=args["body"],
            cc=args.get("cc"),
            bcc=args.get("bcc"),
            html_body=args.get("htmlBody"),
            attachments=args.get("attachments"),
            drafts_folder=args.get("draftsFolder", "Drafts"),
            include_signature=args.get("includeSignature", True),
            idempotency_key=args.get("idempotencyKey"),
        )
    elif name == "update_draft":
        return cli.update_draft(
            uid=args["uid"],
            to=args["to"],
            subject=args["subject"],
            body=args["body"],
            cc=args.get("cc"),
            bcc=args.get("bcc"),
            html_body=args.get("htmlBody"),
            attachments=args.get("attachments"),
            drafts_folder=args.get("draftsFolder", "Drafts"),
            include_signature=args.get("includeSignature", True),
        )
    elif name == "delete_draft":
        return cli.delete_draft(
            uid=args["uid"],
            drafts_folder=args.get("draftsFolder", "Drafts"),
        )
    elif name == "report_spam":
        return cli.report_spam(
            uids=args["uids"],
            mailbox=args.get("mailbox"),
            spam_folder=args.get("spamFolder"),
            flag=args.get("flag"),
        )
    elif name == "mark_not_spam":
        return cli.mark_not_spam(
            uids=args["uids"],
            mailbox=args.get("mailbox"),
            destination=args.get("destination"),
        )

    # ------------------------------------------------------------------
    # Write-mode actions
    # ------------------------------------------------------------------
    elif name == "send_email":
        return cli.send_email(
            to=args["to"], subject=args["subject"], body=args["body"],
            cc=args.get("cc"), bcc=args.get("bcc"),
            html_body=args.get("htmlBody"),
            attachments=args.get("attachments"),
            include_signature=args.get("includeSignature", True),
            save_to_sent=args.get("saveToSent", True),
            sent_folder=args.get("sentFolder"),
            idempotency_key=args.get("idempotencyKey"),
        )
    elif name == "reply_email":
        return cli.reply_email(
            uid=args["uid"], body=args["body"],
            mailbox=args.get("mailbox"),
            html_body=args.get("htmlBody"),
            reply_all=args.get("replyAll", False),
            attachments=args.get("attachments"),
            include_signature=args.get("includeSignature", True),
            quote_original=args.get("quoteOriginal", True),
            save_to_sent=args.get("saveToSent", True),
            idempotency_key=args.get("idempotencyKey"),
        )
    elif name == "forward_email":
        return cli.forward_email(
            uid=args["uid"], to=args["to"], body=args.get("body", ""),
            mailbox=args.get("mailbox"),
            cc=args.get("cc"), bcc=args.get("bcc"),
            html_body=args.get("htmlBody"),
            include_attachments=args.get("includeAttachments", True),
            extra_attachments=args.get("extraAttachments"),
            include_signature=args.get("includeSignature", True),
            save_to_sent=args.get("saveToSent", True),
            idempotency_key=args.get("idempotencyKey"),
        )
    elif name == "delete_email":
        return cli.delete_email(
            uids=args["uids"],
            mailbox=args.get("mailbox"),
            permanent=args.get("permanent", False),
            trash_folder=args.get("trashFolder"),
        )

    # ------------------------------------------------------------------
    # Statistics, cache, watch, sync (unchanged)
    # ------------------------------------------------------------------
    elif name == "get_unread_count":
        return cli.get_unread_count(mailbox=args.get("mailbox", "INBOX"))
    elif name == "get_total_count":
        return cli.get_total_count(mailbox=args.get("mailbox", "INBOX"))
    elif name == "get_cached_overview":
        return cli.get_cached_overview(
            mailbox=args.get("mailbox"), limit=args.get("limit", 20)
        )
    elif name == "refresh_cache":
        return cli.refresh_cache()
    elif name == "start_watch":
        return cli.start_watch()
    elif name == "stop_watch":
        return cli.stop_watch()
    elif name == "idle_watch":
        return cli.idle_watch(
            mailbox=args.get("mailbox", "INBOX"),
            timeout=args.get("timeout", 300),
        )
    elif name == "sync_emails":
        return cli.sync_emails(
            mailbox=args.get("mailbox", "INBOX"),
            since=args.get("since"),
            before=args.get("before"),
            full=args.get("full", False),
        )
    elif name == "load_cache":
        return cli.load_cache(
            mailbox=args.get("mailbox", "INBOX"),
            mode=args.get("mode", "recent"),
            count=args.get("count", 100),
            since=args.get("since"),
            before=args.get("before"),
            include_attachments=args.get("include_attachments", True),
        )
    elif name == "get_cache_stats":
        return cli.get_cache_stats()
    elif name == "cleanup_sent_log":
        return cli.cleanup_sent_log(
            older_than_days=int(args.get("older_than_days", 30))
        )
    elif name == "audit_log_query":
        if not cli.email_cache:
            raise RuntimeError("Persistent cache is disabled.")
        return cli.email_cache.query_audit_log(
            limit=int(args.get("limit", 100)),
            tool=args.get("tool"),
            write_only=bool(args.get("write_only", False)),
            since=args.get("since"),
        )
    elif name == "cleanup_audit_log":
        if not cli.email_cache:
            raise RuntimeError("Persistent cache is disabled.")
        return cli.email_cache.cleanup_audit_log(
            older_than_days=int(args.get("older_than_days", 90))
        )
    elif name == "get_email_auth_results":
        return cli.get_email_auth_results(
            uid=args["uid"], mailbox=args.get("mailbox"),
        )
    elif name == "vacuum_cache":
        return cli.vacuum_cache()
    elif name == "export_cache":
        return cli.export_cache(
            passphrase=args["passphrase"],
            output_path=args["output_path"],
        )
    elif name == "import_cache":
        return cli.import_cache(
            passphrase=args["passphrase"],
            input_path=args["input_path"],
        )
    elif name == "rotate_encryption_key":
        return cli.rotate_encryption_key()

    # ------------------------------------------------------------------
    # Server metadata
    # ------------------------------------------------------------------
    elif name == "get_capabilities":
        return cli.get_capabilities()
    elif name == "get_namespace":
        return cli.get_namespace()
    elif name == "get_quota":
        return cli.get_quota(mailbox=args.get("mailbox"))
    elif name == "get_server_id":
        return cli.get_server_id()

    # ------------------------------------------------------------------
    # Auto-archive
    # ------------------------------------------------------------------
    elif name == "get_auto_archive_list":
        return cli.get_auto_archive_list()
    elif name == "add_auto_archive_sender":
        return cli.add_auto_archive_sender(
            email_addr=args["email"], comment=args.get("comment")
        )
    elif name == "remove_auto_archive_sender":
        return cli.remove_auto_archive_sender(email_addr=args["email"])
    elif name == "reload_auto_archive":
        return cli.reload_auto_archive()
    elif name == "process_auto_archive":
        return cli.process_auto_archive(dry_run=args.get("dry_run", False))

    # ------------------------------------------------------------------
    # Sieve (ManageSieve, RFC 5804)
    # ------------------------------------------------------------------
    elif name == "sieve_list_scripts":
        return _sieve_call(account, lambda c: c.listscripts())
    elif name == "sieve_get_script":
        return _sieve_call(account, lambda c: {"name": args["name"], "content": c.getscript(args["name"])})
    elif name == "sieve_check_script":
        # CHECKSCRIPT is server-side validation; account context still needed
        # for the connection but it doesn't mutate any state.
        return _sieve_call(account, lambda c: c.checkscript(args["content"]))
    elif name == "sieve_put_script":
        return _sieve_call(
            account,
            lambda c: (c.putscript(args["name"], args["content"]),
                       {"uploaded": args["name"]})[-1],
        )
    elif name == "sieve_delete_script":
        return _sieve_call(
            account,
            lambda c: (c.deletescript(args["name"]),
                       {"deleted": args["name"]})[-1],
        )
    elif name == "sieve_activate_script":
        return _sieve_call(
            account,
            lambda c: (c.setactive(args["name"]),
                       {"active": args["name"] or None})[-1],
        )

    else:
        raise ValueError(f"Unknown tool: {name}")


# ============================================================================
# MCP resources
#
# We expose three URI shapes per account:
#
#   imap://{account}/overview          -- inbox-style summary as text/markdown
#   imap://{account}/{mailbox}/{uid}   -- one email rendered as text
#   imap://{account}/health            -- per-account health snapshot
#
# Static resources (one ``overview`` and one ``health`` per configured
# account) are listed via ``list_resources``. Per-message URLs are declared
# via ``list_resource_templates`` so MCP clients know the URI shape and can
# request anything matching it.
# ============================================================================


def _account_overview_resources() -> list[Resource]:
    out: list[Resource] = []
    for acct in account_manager.accounts.values():
        out.append(Resource(
            name=f"{acct.name}: inbox overview",
            uri=f"imap://{acct.name}/overview",
            mimeType="text/markdown",
            description=(
                f"Recent unread/flagged summary for {acct.name} "
                f"(populated from cache when available)."
            ),
        ))
        out.append(Resource(
            name=f"{acct.name}: health",
            uri=f"imap://{acct.name}/health",
            mimeType="application/json",
            description=f"Per-account NOOP + cache/watcher health for {acct.name}.",
        ))
    return out


@server.list_resources()
async def list_resources() -> list[Resource]:
    if not account_manager.has_accounts():
        return []
    return _account_overview_resources()


@server.list_resource_templates()
async def list_resource_templates() -> list[ResourceTemplate]:
    return [
        ResourceTemplate(
            name="Single email",
            uriTemplate="imap://{account}/{mailbox}/{uid}",
            description=(
                "Read one email by UID. Returns headers + plain-text body "
                "(HTML emails are converted via html2text)."
            ),
            mimeType="text/markdown",
        ),
        ResourceTemplate(
            name="Mailbox summary",
            uriTemplate="imap://{account}/{mailbox}/summary",
            description=(
                "Compact summary list (subject/from/date/snippet) of the "
                "20 most recent UIDs in a mailbox."
            ),
            mimeType="text/markdown",
        ),
    ]


def _parse_imap_uri(uri: str) -> tuple[str, list[str]]:
    """Return ``(account, [parts...])`` for an imap:// URI.

    ``imap://work/INBOX/4231``      -> ("work", ["INBOX", "4231"])
    ``imap://work/INBOX/summary``   -> ("work", ["INBOX", "summary"])
    ``imap://work/overview``        -> ("work", ["overview"])
    """
    if not uri.startswith("imap://"):
        raise ValueError(f"Unsupported URI scheme: {uri!r}")
    rest = uri[len("imap://"):]
    if not rest:
        raise ValueError("Empty IMAP URI")
    parts = [p for p in rest.split("/") if p]
    if not parts:
        raise ValueError(f"Malformed IMAP URI: {uri!r}")
    return parts[0], parts[1:]


def _format_overview(cli) -> str:
    """Render an inbox overview as compact markdown."""
    try:
        unread = cli.search_unread(mailbox="INBOX", limit=20)
    except Exception as exc:
        return f"# Inbox overview\n\n_error: {exc}_"
    lines = [f"# Inbox overview ({len(unread)} unread shown)\n"]
    for h in unread:
        sender = (h.from_address.email if h.from_address else "?")
        date = h.date.isoformat() if h.date else "?"
        lines.append(f"- **{h.subject or '(no subject)'}** -- {sender} ({date})")
    return "\n".join(lines)


def _format_email(cli, mailbox: str, uid: int) -> str:
    """Render a single email as markdown."""
    try:
        msg = cli.get_email(uid=uid, mailbox=mailbox)
    except Exception as exc:
        return f"_error fetching {mailbox}/{uid}: {exc}_"
    h = msg.header
    body_text = (msg.body.text if msg.body else "") or ""
    md = (
        f"# {h.subject or '(no subject)'}\n\n"
        f"- **From:** {h.from_address.email if h.from_address else '?'}\n"
        f"- **To:** {', '.join(a.email for a in h.to_addresses) or '?'}\n"
        f"- **Date:** {h.date.isoformat() if h.date else '?'}\n"
        f"- **Flags:** {', '.join(h.flags) or '(none)'}\n"
        f"- **Attachments:** {len(msg.attachments)}\n\n"
        f"---\n\n{body_text}"
    )
    return md


def _format_mailbox_summary(cli, mailbox: str) -> str:
    """Render a 20-email mailbox summary as markdown."""
    try:
        headers = cli.fetch_emails(mailbox=mailbox, limit=20)
        uids = [h.uid for h in headers]
        summaries = cli.get_email_summary(uids=uids, mailbox=mailbox, body_chars=200)
    except Exception as exc:
        return f"_error: {exc}_"
    lines = [f"# {mailbox}: {len(summaries)} most recent\n"]
    for s in summaries:
        marker = "·" if s.get("unread") else " "
        lines.append(
            f"- {marker} **{s.get('subject') or '(no subject)'}** -- "
            f"{s.get('sender') or '?'} ({s.get('date') or '?'})"
        )
        snippet = (s.get("snippet") or "").replace("\n", " ").strip()
        if snippet:
            lines.append(f"  > {snippet}")
    return "\n".join(lines)


# In-memory subscription registry. The watcher thread can't reach the
# active session directly, so it pushes account names into this set; the
# next chance the server has to notify (next call_tool, etc.) it picks
# them up. Per-account dirty-flag keeps notification cost O(1).
_subscribed_uris: set[str] = set()
_dirty_overviews: set[str] = set()


def _mark_overview_dirty(account_name: str) -> None:
    """Called by the IDLE watcher when a watched mailbox changes."""
    _dirty_overviews.add(account_name)


def _wire_resource_subscriptions() -> None:
    """Attach the dirty-flag callback to every account's watcher."""
    for acct in account_manager.accounts.values():
        watcher = getattr(acct.client, "watcher", None)
        if watcher is None:
            continue
        # Wrap the existing on_update so we don't clobber it.
        prior = watcher.on_update
        def _make(name):
            def _cb(key, cache):
                _mark_overview_dirty(name)
                if prior:
                    try:
                        prior(key, cache)
                    except Exception:
                        pass
            return _cb
        watcher.on_update = _make(acct.name)


@server.subscribe_resource()
async def subscribe_resource(uri) -> None:
    """Track which resources the client is watching for updates."""
    _subscribed_uris.add(str(uri))


@server.unsubscribe_resource()
async def unsubscribe_resource(uri) -> None:
    _subscribed_uris.discard(str(uri))


async def _flush_resource_notifications() -> None:
    """If the watcher marked any overview dirty, notify subscribed clients."""
    if not _dirty_overviews or not _subscribed_uris:
        return
    try:
        session = server.request_context.session
    except (LookupError, AttributeError):
        return
    for account_name in list(_dirty_overviews):
        uri = f"imap://{account_name}/overview"
        if uri in _subscribed_uris:
            try:
                await session.send_resource_updated(uri)
            except Exception:
                pass
    _dirty_overviews.clear()


@server.read_resource()
async def read_resource(uri):
    """Resolve an ``imap://...`` URI into renderable text."""
    uri_str = str(uri)
    account, parts = _parse_imap_uri(uri_str)
    cli = _client(account)

    if not parts:
        raise ValueError(f"Malformed URI: {uri_str!r}")

    # imap://{account}/overview
    if len(parts) == 1 and parts[0] == "overview":
        return _format_overview(cli)
    # imap://{account}/health
    if len(parts) == 1 and parts[0] == "health":
        return json.dumps(cli.health_check(), default=str, ensure_ascii=False)
    # imap://{account}/{mailbox}/summary
    if len(parts) == 2 and parts[1] == "summary":
        return _format_mailbox_summary(cli, parts[0])
    # imap://{account}/{mailbox}/{uid}
    if len(parts) == 2:
        mailbox, uid_str = parts
        try:
            uid = int(uid_str)
        except ValueError as exc:
            raise ValueError(f"Bad UID in URI {uri_str!r}: {exc}")
        return _format_email(cli, mailbox, uid)

    raise ValueError(f"Unhandled IMAP URI shape: {uri_str!r}")


# ============================================================================
# MCP prompts
#
# Pre-baked prompt templates for the most common AI workflows. Each prompt
# returns a single user-role message; the client substitutes placeholders.
# ============================================================================


_PROMPTS: dict[str, dict] = {
    "summarize_inbox": {
        "title": "Summarize unread inbox",
        "description": "Produce a concise digest of unread emails in INBOX.",
        "arguments": [
            PromptArgument(name="account", description="Account name (optional)", required=False),
            PromptArgument(name="limit", description="Max emails to consider (default 20)", required=False),
        ],
        "template": (
            "Use the imap-mcp tools to summarize the inbox.\n\n"
            "1. Call `search_unread(account={account}, mailbox=\"INBOX\", limit={limit})`.\n"
            "2. Call `get_email_summary(account={account}, uids=[...], body_chars=300)` "
            "with the UIDs from step 1.\n"
            "3. Group the results by sender domain and produce a 5-10 line "
            "executive digest. Highlight anything that requires a response."
        ),
    },
    "triage_inbox": {
        "title": "Triage unread inbox",
        "description": (
            "Sort unread emails into action / waiting / archive buckets and "
            "propose moves (preview only -- ask before mutating)."
        ),
        "arguments": [
            PromptArgument(name="account", description="Account name (optional)", required=False),
        ],
        "template": (
            "Triage the unread inbox for account {account}.\n\n"
            "1. `search_unread(account={account}, mailbox=\"INBOX\", limit=50)`.\n"
            "2. `get_email_summary(account={account}, uids=[...])`.\n"
            "3. For each email, classify: ACTION / WAITING / ARCHIVE / SPAM.\n"
            "4. For ACTION: list a one-line follow-up.\n"
            "5. For ARCHIVE/SPAM: propose a `bulk_action(..., dry_run=true)` "
            "that would move them. **Wait for explicit user approval before "
            "running the call without dry_run.**"
        ),
    },
    "draft_reply": {
        "title": "Draft a reply",
        "description": "Draft (don't send) a reply to a specific UID.",
        "arguments": [
            PromptArgument(name="account", description="Account name", required=False),
            PromptArgument(name="uid", description="Email UID to reply to", required=True),
            PromptArgument(name="tone", description="Tone (formal/casual/concise)", required=False),
        ],
        "template": (
            "Draft a {tone} reply to UID {uid} on account {account}.\n\n"
            "1. Read the original via `get_email(account={account}, uid={uid})`.\n"
            "2. Draft a response addressing every question or action item.\n"
            "3. Save it via `save_draft(account={account}, to=[<reply-to>], "
            "subject=\"Re: <orig>\", body=...)`. Do not call `reply_email` -- "
            "leave it as a draft for the user to review."
        ),
    },
    "extract_action_items": {
        "title": "Extract action items from an email",
        "description": "Pull explicit and implicit action items out of one email.",
        "arguments": [
            PromptArgument(name="account", description="Account name", required=False),
            PromptArgument(name="uid", description="Email UID", required=True),
        ],
        "template": (
            "Extract action items from UID {uid} on account {account}.\n\n"
            "1. `get_email(account={account}, uid={uid})`.\n"
            "2. List explicit asks (\"please send X by Friday\") and implicit "
            "ones (deadlines mentioned, blockers raised).\n"
            "3. For each, suggest the owner and a due date when knowable. "
            "Return as a markdown checklist."
        ),
    },
    "find_similar_emails": {
        "title": "Find similar / related emails",
        "description": "Use FTS5 to find emails related to a topic.",
        "arguments": [
            PromptArgument(name="account", description="Account name", required=False),
            PromptArgument(name="query", description="Free-text query", required=True),
            PromptArgument(name="limit", description="Max results (default 20)", required=False),
        ],
        "template": (
            "Search the local FTS5 index for {query!r} on account {account}.\n\n"
            "1. `search_emails_fts(account={account}, query={query!r}, "
            "limit={limit})`.\n"
            "2. `get_email_summary(account={account}, uids=[...])` for context.\n"
            "3. Group results by sender and time, surface the top 5 most "
            "relevant with one-line context each."
        ),
    },
}


@server.list_prompts()
async def list_prompts() -> list[Prompt]:
    return [
        Prompt(
            name=key,
            title=p["title"],
            description=p["description"],
            arguments=p.get("arguments", []),
        )
        for key, p in _PROMPTS.items()
    ]


@server.get_prompt()
async def get_prompt(name: str, arguments: Optional[dict] = None) -> GetPromptResult:
    prompt = _PROMPTS.get(name)
    if prompt is None:
        raise ValueError(f"Unknown prompt: {name!r}")

    args = dict(arguments or {})
    args.setdefault("account", account_manager.default_name or "<default>")
    args.setdefault("limit", 20)
    args.setdefault("tone", "concise")
    args.setdefault("uid", "<UID>")
    args.setdefault("query", "<query>")

    text = prompt["template"].format(**args)
    return GetPromptResult(
        description=prompt["description"],
        messages=[PromptMessage(role="user", content=TextContent(type="text", text=text))],
    )


def _sieve_call(account: Optional[str], fn):
    """Open a short-lived ManageSieve connection for ``account`` and run ``fn``."""
    from .sieve import open_for, SieveError
    acct = account_manager.get_account(account)
    try:
        client = open_for(acct.config, action="manage")
    except SieveError as exc:
        raise RuntimeError(str(exc))
    try:
        return fn(client)
    finally:
        client.logout()


def main():
    """Main entry point for the MCP server."""
    import argparse

    parser = argparse.ArgumentParser(description="IMAP MCP Server")
    parser.add_argument(
        "--set-password",
        action="store_true",
        help="Store IMAP password in the OS keyring (macOS Keychain / "
             "Windows Credential Locker / Linux SecretService)",
    )
    parser.add_argument(
        "--delete-password",
        action="store_true",
        help="Remove stored password from the OS keyring",
    )
    parser.add_argument(
        "--config",
        default="config.json",
        help="Path to config.json (default: config.json)",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="Enable write-mode tools (send_email, reply_email, forward_email, "
             "delete_email, rename_mailbox, delete_mailbox, empty_mailbox, "
             "sieve_put_script/delete_script/activate_script). Without this "
             "flag the server is read-only -- only reading and organizing "
             "operations are exposed.",
    )
    parser.add_argument(
        "--account",
        default=None,
        help="Account name (matches accounts[].name). Used together with "
             "--set-password / --delete-password in multi-account configs. "
             "Omit to use the default account.",
    )
    parser.add_argument(
        "--migrate-config",
        action="store_true",
        help="Rewrite a legacy single-account config.json into the new "
             "multi-account format. The original is backed up to "
             "<path>.bak.",
    )
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="Validate config.json structure, look up keyring passwords, "
             "and exit with a clear pass/fail report. Does not start the "
             "MCP server.",
    )
    parser.add_argument(
        "--check-connection",
        action="store_true",
        help="With --check-config, also try to open an IMAP socket per "
             "account (slower; does network IO).",
    )
    parser.add_argument(
        "--init-account",
        metavar="PROVIDER",
        help="Print a starter account block for one of the known providers "
             "(gmail, outlook, fastmail, proton, icloud, yahoo, yandex). "
             "Use together with --account NAME to set the account name.",
    )
    parser.add_argument(
        "--init-account-username",
        metavar="EMAIL",
        help="Username/email for --init-account.",
    )
    parser.add_argument(
        "--print-schema",
        action="store_true",
        help="Print the JSON Schema for config.json to stdout. Pipe to a "
             "file and reference it in your editor for autocomplete.",
    )
    args = parser.parse_args()

    if args.migrate_config:
        from .accounts import migrate_legacy_config
        try:
            backup = migrate_legacy_config(args.config)
        except Exception as exc:
            print(f"Migration failed: {exc}")
            raise SystemExit(1)
        print(f"Migrated. Backup written to {backup}")
        return

    if args.print_schema:
        from .providers import CONFIG_JSON_SCHEMA
        print(json.dumps(CONFIG_JSON_SCHEMA, indent=2, ensure_ascii=False))
        return

    if args.init_account:
        from .providers import make_starter_account, PROVIDER_TEMPLATES
        if args.init_account not in PROVIDER_TEMPLATES:
            print(f"Unknown provider: {args.init_account!r}.")
            print(f"Known: {sorted(PROVIDER_TEMPLATES)}")
            raise SystemExit(1)
        if not args.init_account_username:
            print("Error: --init-account requires --init-account-username EMAIL")
            raise SystemExit(1)
        account_name = args.account or args.init_account
        block = make_starter_account(
            args.init_account, account_name, args.init_account_username,
        )
        print(json.dumps(block, indent=2, ensure_ascii=False))
        print(
            "\n# Copy the block above into the 'accounts' array of "
            "your config.json.\n"
            f"# Then run: imap-mcp --set-password --config <path> --account {account_name}",
            file=__import__("sys").stderr,
        )
        return

    if args.check_config:
        from .providers import validate_config
        result = validate_config(
            args.config,
            check_connection=args.check_connection,
        )
        if result["valid"]:
            print(f"OK: {args.config}")
        else:
            print(f"INVALID: {args.config}")
        for err in result.get("errors", []):
            print(f"  ERROR: {err}")
        for warn in result.get("warnings", []):
            print(f"  WARN:  {warn}")
        for acct in result.get("accounts", []):
            status = "ok" if acct["ok"] else "FAIL"
            print(f"  account {acct['name']!r}: {status}")
            for err in acct.get("errors", []):
                print(f"    ERROR: {err}")
            for warn in acct.get("warnings", []):
                print(f"    WARN:  {warn}")
        raise SystemExit(0 if result["valid"] else 1)

    if args.set_password or args.delete_password:
        from .accounts import AccountManager as _AM
        from .imap_client import store_password, delete_stored_password
        import getpass

        with open(args.config) as f:
            raw = json.load(f)

        if "accounts" not in raw:
            if _AM.is_legacy_config(raw):
                print(
                    "Config uses legacy single-account format. "
                    "Run 'imap-mcp --migrate-config --config %s' first." % args.config
                )
                raise SystemExit(1)
            print("Error: 'accounts' missing from config.json")
            raise SystemExit(1)

        accounts = raw["accounts"]
        if not accounts:
            print("Error: config.accounts is empty")
            raise SystemExit(1)

        target = None
        if args.account:
            for a in accounts:
                if a.get("name") == args.account:
                    target = a
                    break
            if target is None:
                print(f"Error: no account named {args.account!r} in config.")
                raise SystemExit(1)
        elif len(accounts) == 1:
            target = accounts[0]
        else:
            defaults = [a for a in accounts if a.get("default")]
            if len(defaults) == 1:
                target = defaults[0]
            else:
                print(
                    "Multiple accounts in config; pass --account <name> to "
                    "select which one."
                )
                raise SystemExit(1)

        username = target.get("credentials", {}).get("username", "")
        if not username:
            print("Error: account is missing credentials.username")
            raise SystemExit(1)

        if args.delete_password:
            delete_stored_password(username)
            print(f"Password deleted from keyring for {username}")
            return

        password = getpass.getpass(f"Enter IMAP password for {username}: ")
        imap_config = target.get("imap", {})
        host = imap_config.get("host")
        port = imap_config.get("port", 993)
        secure = imap_config.get("secure", True)
        print(f"Verifying connection to {host}:{port}...")
        try:
            from imapclient import IMAPClient
            test_client = IMAPClient(host, port=port, ssl=secure)
            test_client.login(username, password)
            test_client.logout()
        except Exception as e:
            print(f"Connection failed: {e}")
            print("Password was NOT saved.")
            raise SystemExit(1)
        store_password(username, password)
        print(f"Connection OK. Password stored in keyring for {username}")
        return

    global _config_path, _write_enabled
    _config_path = args.config
    _write_enabled = bool(args.write)

    # Eagerly load the config so accounts with cache.enabled=true start
    # their IDLE watchers immediately, mirroring the previous single-account
    # auto_connect behaviour.
    try:
        account_manager.load_config(_config_path)
    except FileNotFoundError as exc:
        print(f"Warning: {exc}. Use 'auto_connect' tool to load it later.")
    except ValueError as exc:
        print(f"Config error: {exc}")
        raise SystemExit(1)

    asyncio.run(run_server())


async def run_server():
    """Run the MCP server."""
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    main()
