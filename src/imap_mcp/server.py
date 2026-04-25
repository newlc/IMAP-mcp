"""IMAP MCP Server -- Model Context Protocol server for IMAP email operations.

Exposes IMAP capabilities (reading, searching, moving, caching, watching)
as MCP tools that can be consumed by any MCP-compatible client.
"""

import asyncio
import json
from typing import Any, Optional

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

from .imap_client import ImapClientWrapper


# Global IMAP client instance
imap_client = ImapClientWrapper()

# Config path (set from CLI --config argument)
_config_path = "config.json"

# Create MCP server
server = Server("imap-mcp")


def make_tool(
    name: str,
    description: str,
    properties: dict,
    required: Optional[list[str]] = None,
) -> Tool:
    """Helper to create a Tool definition with a JSON Schema input."""
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
    """List all available IMAP tools."""
    return [
        # === Connection ===
        make_tool(
            "connect",
            "Establish IMAP connection to mail server",
            {
                "host": {"type": "string", "description": "IMAP server hostname"},
                "port": {"type": "number", "description": "IMAP port (default: 993)"},
                "secure": {"type": "boolean", "description": "Use SSL/TLS (default: true)"},
            },
            ["host"],
        ),
        make_tool(
            "authenticate",
            "Login with username and password",
            {
                "username": {"type": "string", "description": "Email username"},
                "password": {"type": "string", "description": "Email password or app password"},
                "smtpHost": {"type": "string", "description": "SMTP server hostname (optional, for drafts)"},
                "smtpPort": {"type": "number", "description": "SMTP port (default: 587)"},
            },
            ["username", "password"],
        ),
        make_tool(
            "disconnect",
            "Close IMAP connection",
            {},
        ),
        make_tool(
            "auto_connect",
            "Connect using config.json credentials (no parameters needed)",
            {},
        ),
        # === Mailboxes ===
        make_tool(
            "list_mailboxes",
            "List all mailbox folders",
            {
                "pattern": {"type": "string", "description": "Filter pattern (optional)"},
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
            "Get complete email by UID",
            {
                "uid": {"type": "number", "description": "Email UID"},
                "mailbox": {"type": "string", "description": "Mailbox name (default: current)"},
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
                "draftsFolder": {"type": "string", "description": "Drafts folder name (default: Drafts)"},
                "includeSignature": {"type": "boolean", "description": "Include signature from config (default: true)"},
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
    ]


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
        return [TextContent(type="text", text=serialize_result(result))]
    except Exception as e:
        return [TextContent(type="text", text=json.dumps({"error": str(e)}))]


async def handle_tool_call(name: str, args: dict) -> Any:
    """Route tool calls to appropriate handler."""

    # === Connection ===
    if name == "connect":
        return imap_client.connect(
            host=args["host"],
            port=args.get("port", 993),
            secure=args.get("secure", True),
        )
    elif name == "authenticate":
        return imap_client.authenticate(
            username=args["username"],
            password=args["password"],
        )
    elif name == "disconnect":
        return imap_client.disconnect()
    elif name == "auto_connect":
        return imap_client.auto_connect(_config_path)

    # === Mailboxes ===
    elif name == "list_mailboxes":
        return imap_client.list_mailboxes(pattern=args.get("pattern", "*"))
    elif name == "select_mailbox":
        return imap_client.select_mailbox(args["mailbox"])
    elif name == "create_mailbox":
        return imap_client.create_mailbox(args["mailbox"])
    elif name == "get_mailbox_status":
        return imap_client.get_mailbox_status(args["mailbox"])

    # === Email Reading ===
    elif name == "fetch_emails":
        return imap_client.fetch_emails(
            mailbox=args.get("mailbox"),
            limit=args.get("limit", 20),
            offset=args.get("offset", 0),
            since=args.get("since"),
            before=args.get("before"),
        )
    elif name == "get_email":
        return imap_client.get_email(
            uid=args["uid"],
            mailbox=args.get("mailbox"),
        )
    elif name == "get_email_headers":
        return imap_client.get_email_headers(
            uid=args["uid"],
            mailbox=args.get("mailbox"),
        )
    elif name == "get_email_body":
        return imap_client.get_email_body(
            uid=args["uid"],
            mailbox=args.get("mailbox"),
            format=args.get("format", "text"),
        )
    elif name == "get_attachments":
        return imap_client.get_attachments(
            uid=args["uid"],
            mailbox=args.get("mailbox"),
        )
    elif name == "download_attachment":
        filename, content_type, data = imap_client.download_attachment(
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
        return imap_client.get_thread(
            uid=args["uid"],
            mailbox=args.get("mailbox"),
        )

    # === Search ===
    elif name == "search_emails":
        return imap_client.search_emails(
            query=args["query"],
            mailbox=args.get("mailbox"),
            limit=args.get("limit", 50),
        )
    elif name == "search_by_sender":
        return imap_client.search_by_sender(
            sender=args["sender"],
            mailbox=args.get("mailbox"),
            limit=args.get("limit", 50),
        )
    elif name == "search_by_subject":
        return imap_client.search_by_subject(
            subject=args["subject"],
            mailbox=args.get("mailbox"),
            limit=args.get("limit", 50),
        )
    elif name == "search_by_date":
        return imap_client.search_by_date(
            mailbox=args.get("mailbox"),
            since=args.get("since"),
            before=args.get("before"),
            limit=args.get("limit", 50),
        )
    elif name == "search_unread":
        return imap_client.search_unread(
            mailbox=args.get("mailbox"),
            limit=args.get("limit", 50),
        )
    elif name == "search_flagged":
        return imap_client.search_flagged(
            mailbox=args.get("mailbox"),
            limit=args.get("limit", 50),
        )

    # === Actions ===
    elif name == "mark_read":
        return imap_client.mark_read(
            uids=args["uids"],
            mailbox=args.get("mailbox"),
        )
    elif name == "mark_unread":
        return imap_client.mark_unread(
            uids=args["uids"],
            mailbox=args.get("mailbox"),
        )
    elif name == "flag_email":
        return imap_client.flag_email(
            uids=args["uids"],
            flag=args["flag"],
            mailbox=args.get("mailbox"),
        )
    elif name == "unflag_email":
        return imap_client.unflag_email(
            uids=args["uids"],
            flag=args["flag"],
            mailbox=args.get("mailbox"),
        )
    elif name == "move_email":
        return imap_client.move_email(
            uids=args["uids"],
            destination=args["destination"],
            mailbox=args.get("mailbox"),
        )
    elif name == "copy_email":
        return imap_client.copy_email(
            uids=args["uids"],
            destination=args["destination"],
            mailbox=args.get("mailbox"),
        )
    elif name == "archive_email":
        return imap_client.archive_email(
            uids=args["uids"],
            mailbox=args.get("mailbox"),
            archive_folder=args.get("archiveFolder", "Archive"),
        )
    elif name == "save_draft":
        return imap_client.save_draft(
            to=args["to"],
            subject=args["subject"],
            body=args["body"],
            cc=args.get("cc"),
            bcc=args.get("bcc"),
            html_body=args.get("htmlBody"),
            drafts_folder=args.get("draftsFolder", "Drafts"),
            include_signature=args.get("includeSignature", True),
        )

    # === Statistics ===
    elif name == "get_unread_count":
        return imap_client.get_unread_count(
            mailbox=args.get("mailbox", "INBOX"),
        )
    elif name == "get_total_count":
        return imap_client.get_total_count(
            mailbox=args.get("mailbox", "INBOX"),
        )

    # === Cache & Watch ===
    elif name == "get_cached_overview":
        return imap_client.get_cached_overview(
            mailbox=args.get("mailbox"),
            limit=args.get("limit", 20),
        )
    elif name == "refresh_cache":
        return imap_client.refresh_cache()
    elif name == "start_watch":
        return imap_client.start_watch()
    elif name == "stop_watch":
        return imap_client.stop_watch()
    elif name == "idle_watch":
        return imap_client.idle_watch(
            mailbox=args.get("mailbox", "INBOX"),
            timeout=args.get("timeout", 300),
        )

    # === Sync & Persistent Cache ===
    elif name == "sync_emails":
        return imap_client.sync_emails(
            mailbox=args.get("mailbox", "INBOX"),
            since=args.get("since"),
            before=args.get("before"),
            full=args.get("full", False),
        )
    elif name == "load_cache":
        return imap_client.load_cache(
            mailbox=args.get("mailbox", "INBOX"),
            mode=args.get("mode", "recent"),
            count=args.get("count", 100),
            since=args.get("since"),
            before=args.get("before"),
            include_attachments=args.get("include_attachments", True),
        )
    elif name == "get_cache_stats":
        return imap_client.get_cache_stats()

    # === Auto-Archive ===
    elif name == "get_auto_archive_list":
        return imap_client.get_auto_archive_list()
    elif name == "add_auto_archive_sender":
        return imap_client.add_auto_archive_sender(
            email_addr=args["email"],
            comment=args.get("comment"),
        )
    elif name == "remove_auto_archive_sender":
        return imap_client.remove_auto_archive_sender(
            email_addr=args["email"],
        )
    elif name == "reload_auto_archive":
        return imap_client.reload_auto_archive()
    elif name == "process_auto_archive":
        return imap_client.process_auto_archive(
            dry_run=args.get("dry_run", False),
        )

    else:
        raise ValueError(f"Unknown tool: {name}")


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
    args = parser.parse_args()

    if args.set_password or args.delete_password:
        from .imap_client import store_password, delete_stored_password, ImapClientWrapper
        import getpass

        client = ImapClientWrapper()
        client.load_config(args.config)
        username = client.config.get("credentials", {}).get("username", "")
        if not username:
            print("Error: no credentials.username in config.json")
            raise SystemExit(1)

        if args.delete_password:
            delete_stored_password(username)
            print(f"Password deleted from keyring for {username}")
        else:
            password = getpass.getpass(f"Enter IMAP password for {username}: ")

            # Verify the password by connecting to the IMAP server
            imap_config = client.config.get("imap", {})
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

    global _config_path
    _config_path = args.config
    asyncio.run(run_server())


async def run_server():
    """Run the MCP server."""
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    main()
