# IMAP-MCP

[![CI](https://github.com/newlc/IMAP-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/newlc/IMAP-mcp/actions/workflows/ci.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![MCP](https://img.shields.io/badge/MCP-compatible-purple.svg)](https://modelcontextprotocol.io)

MCP server for IMAP email operations with persistent encrypted cache. Designed for AI assistants (Claude Code, Claude Desktop) to read, search, organize, and draft emails on your behalf.

## Why IMAP-MCP?

Most email MCP servers rely on AppleScript or native mail client APIs, which choke on large mailboxes (100K+ emails) and require the mail app to be running. IMAP-MCP talks directly to the IMAP server and caches everything locally in SQLite, so you can:

- **Work with massive mailboxes (300K+ emails)** without timeouts or memory issues -- the cache handles pagination and incremental loading
- **Analyze email offline** -- once cached, all queries run against the local database with zero server load, making it fast even on slow or metered connections
- **Feed email data to other tools** -- use cached emails as context for AI assistants to create tasks from action items, track unanswered emails, summarize threads, generate follow-up drafts, extract contacts, or build dashboards
- **Process email at scale** -- sort thousands of emails into folders, bulk-archive by sender, detect patterns across months of correspondence -- all without hammering your IMAP server

The local cache turns your mailbox into a queryable database that AI assistants can explore freely without worrying about rate limits, connection timeouts, or server availability.

## Features

- **Full IMAP access** -- read, search, move, copy, flag, archive, draft, send, reply, forward, and delete emails
- **Multi-account in one server** -- every account has its own independent (optionally encrypted) cache; tools take an `account` parameter
- **Read-only by default, opt-in `--write` mode** for sending, replying, forwarding, deleting, mailbox rename/delete/empty, and Sieve script management (see [Read-only vs write mode](#read-only-vs-write-mode))
- **Folder management** -- create, rename, delete, empty, subscribe, unsubscribe
- **Drafts** -- save, update, delete (with file attachments)
- **Server-side rules via ManageSieve** (RFC 5804) -- list, get, check, put, delete, activate Sieve scripts
- **Spam handling** -- `report_spam`/`mark_not_spam` (move + `$Junk`/`$NotJunk` keywords for filter training)
- **Safe HTML rendering** -- `get_email_body_safe` runs bleach with a tight whitelist, drops `<script>`/`<style>`/event handlers/`javascript:` URLs, optionally blocks remote images and rewrites links
- **Inline `cid:` images** are extracted and inlined as `data:` URIs so HTML bodies render standalone
- **Calendar invites** -- `get_calendar_invites` parses `text/calendar` parts (method, organizer, attendees, start/end, location)
- **AI-friendly summaries** -- `get_email_summary(uids[])` returns one compact list (subject, sender, date, snippet, has_attachments) in a single round trip
- **Bulk operations by query** -- `bulk_action(action, …)` matches messages by sender/subject/date/unread/flagged in one call and applies mark_read, flag, archive, move, copy, delete, or report_spam
- **Transient-error retries** -- IMAP connect and SMTP send retry on timeouts and connection drops with exponential backoff
- **Per-account health check** -- `accounts_health` issues a NOOP per connected account and reports cache state
- **Per-account locking + connection keepalive** -- two concurrent tool calls can't open duplicate sockets, and a background NOOP every ~20 min stops the IMAP server from dropping the main connection during long agent sessions
- **Watcher reconnect jitter** -- IDLE watchers stagger their initial connect (0-2 s) and back off with jittered exponential delay so a network flap doesn't trigger a thundering herd
- **MCP resources** -- accounts and emails are exposed as `imap://{account}/overview`, `imap://{account}/health`, and `imap://{account}/{mailbox}/{uid}` resources, so MCP clients can cite them directly
- **MCP prompts** -- pre-baked workflow prompts (`summarize_inbox`, `triage_inbox`, `draft_reply`, `extract_action_items`, `find_similar_emails`)
- **Cache maintenance** -- `cleanup_sent_log` purges old idempotency entries, `vacuum_cache` compacts the database, `rotate_encryption_key` *(--write)* re-encrypts the on-disk snapshot with a fresh Fernet key (with previous key backed up under `<keyring_username>.previous`)
- **Idempotent send/reply/forward** -- pass an `idempotencyKey` and a retry after a network blip won't re-send the message
- **Attachment safety** -- `security.max_attachment_size_mb` (default 25 MB) is always enforced; opt-in `security.attachments_allowed_dirs` allowlist blocks prompt-injection from attaching files outside whitelisted folders (symlinks resolved before the check)
- **HTML-only fallback** -- when an email has no `text/plain` part, the cache, FTS index and snippets are populated from `html2text`-converted HTML
- **Partial body fetch** -- `get_email_summary` uses `BODY.PEEK[TEXT]<0.N>` (RFC 3501 partial FETCH) so summarising 100 large emails costs ~100 KiB instead of megabytes
- **Bulk-action batching & limits** -- `bulk_action` accepts `limit` (cap acted-on UIDs) and `batch_size` (default 1000) so 50 K-element STORE/MOVE commands don't trip server limits
- **Real threading** -- IMAP `THREAD REFERENCES` with cached `Message-ID`/`References` fallback
- **Full-text search** -- SQLite FTS5 index over cached subjects, bodies, and addresses; combined IMAP `SEARCH` with multiple criteria
- **Server metadata** -- `CAPABILITY`, `NAMESPACE`, `QUOTA`, `ID`
- **Persistent SQLite cache** with optional AES encryption (Fernet, per-account keys) -- emails and attachments stored locally for fast offline access
- **Cross-platform secure credential storage** via OS keyring (macOS Keychain / Windows Credential Locker / Linux SecretService)
- **Flexible cache loading** -- recent N emails, new-only, older (paginate backwards), or date range
- **Cache-first reads** -- subsequent queries served from local cache without hitting the IMAP server
- **IMAP IDLE watching** for real-time notifications across multiple mailboxes (per-account, opt-in via `cache.enabled`)
- **Auto-archive** -- automatically archive emails from configured sender patterns
- **Bulk email sorting** via MCP tools -- move thousands of emails into folders by sender patterns

## Multi-account configuration

The new config format wraps every account in an `accounts` array. Each account has its own IMAP/SMTP/Sieve servers, credentials, folders, cache (with its own encryption key), and auto-archive list. Mark exactly one account with `"default": true` (you can skip the flag if there's only one account).

```json
{
  "accounts": [
    { "name": "work",     "default": true,  "imap": {...}, "smtp": {...}, "credentials": {...}, "cache": {"enabled": true, "encrypt": true} },
    { "name": "personal",                   "imap": {...}, "smtp": {...}, "credentials": {...}, "cache": {"enabled": false, "encrypt": false} }
  ]
}
```

Every tool takes an optional `account` parameter:

```
fetch_emails(account="work", mailbox="INBOX", limit=20)
send_email(account="personal", to=["..."], subject="...", body="...")
search_emails_fts(account="work", query="invoice 2026")
```

Omit `account` to use the one marked `default: true` (or the only account when there's just one).

### Migrating from the old single-account config

```bash
imap-mcp --migrate-config --config /path/to/config.json
# Backup is written to /path/to/config.json.bak
```

This wraps the old top-level `imap` / `smtp` / `credentials` / `user` / `folders` / `cache` / `auto_archive` blocks into `accounts:[{ "name": "default", "default": true, ... }]`.

### Storing passwords with multiple accounts

```bash
imap-mcp --set-password --config /path/to/config.json --account work
imap-mcp --set-password --config /path/to/config.json --account personal
```

Each account's password is keyed by its `credentials.username` in the OS keyring, so they stay independent.

## Read-only vs write mode

By default, the server starts in **read-only mode**: it exposes only tools that read or organize email -- including marking, flagging, moving, copying, archiving, and saving drafts. Tools that send mail externally or delete messages are not exposed at all.

Pass `--write` to enable the four write-mode tools:

| Tool | Description |
|------|-------------|
| `send_email` | Send a new email via SMTP, with optional attachments and Sent-folder copy |
| `reply_email` | Reply (or reply-all) to an email by UID, preserving threading headers |
| `forward_email` | Forward an email by UID, re-attaching original attachments |
| `delete_email` | Move messages to Trash (default) or `\Deleted` + EXPUNGE (with `permanent=true`) |

```bash
# Read-only (default) -- safe for AI assistants to organize without sending
claude mcp add imap-mcp /path/to/imap-mcp -- --config /path/to/config.json

# Read-write -- assistants can send, reply, forward, and delete
claude mcp add imap-mcp /path/to/imap-mcp -- --config /path/to/config.json --write
```

When the server is started without `--write`, write-mode tools are not visible to the MCP client at all (they are not advertised in `list_tools`), and any attempt to invoke them by name returns a permission error.

## Security

### Credentials

All sensitive credentials are stored in the **OS keyring** -- the native secure storage on each platform:

| Platform | Backend |
|----------|---------|
| macOS | Keychain (protected by Touch ID / login password) |
| Windows | Windows Credential Locker |
| Linux | SecretService (GNOME Keyring / KDE Wallet) |

The `credentials.password` field in `config.json` should be left **empty**. Passwords are saved to the keyring via `imap-mcp --set-password` and retrieved automatically at runtime. No plaintext passwords are ever written to disk.

### Encrypted cache

When `cache.encrypt: true`, the local SQLite database containing your emails, bodies, and attachments is **AES-encrypted** (Fernet / AES-128-CBC + HMAC-SHA256):

- The database lives **entirely in memory** while the server runs
- On disk, only an encrypted snapshot exists (`.db.enc`) -- it is unreadable without the key
- The encryption key is auto-generated on first use and stored in the OS keyring under the service `imap-mcp-cache`
- **If someone copies the `.db.enc` file to another machine, they cannot decrypt it** -- the key only exists in the keyring of the original machine
- The encrypted file is updated atomically (temp file + rename) to prevent corruption
- Auto-flush every 50 write operations + forced flush after bulk operations (`load_cache`, `sync_emails`)

When `cache.encrypt: false`, a plain SQLite file is written to disk with WAL journaling. This is recommended for very large mailboxes (100K+ emails) where holding the entire database in memory would consume too much RAM.

### Other protections

- **UIDVALIDITY tracking**: if the IMAP server reassigns UIDs (mailbox recreation), the cache for that mailbox is automatically purged and rebuilt
- **Namespace auto-detection**: the server automatically handles IMAP servers that require `INBOX.` prefix for folder names (e.g., Dovecot, Jino)

### Attachment safety

`send_email`, `reply_email`, `forward_email`, and `save_draft` all accept local file paths in `attachments`. To stop a malicious prompt from convincing an AI agent to attach `~/.ssh/id_rsa` or `/etc/passwd`, configure per-account limits under `security`:

```jsonc
{
  "name": "work",
  "security": {
    "max_attachment_size_mb": 25,                 // default 25; pass 0 to disable
    "attachments_allowed_dirs": [                 // optional allowlist; if absent, any path goes
      "~/Documents",
      "~/Downloads",
      "/srv/share/outgoing"
    ]
  },
  ...
}
```

Symlinks are resolved before the allowlist check, so symlinking from an allowed dir to `~/.ssh` is rejected.

### Send-time idempotency

`send_email` / `reply_email` / `forward_email` accept an `idempotencyKey` argument. When the persistent cache is enabled, the first successful call writes `(key, message_id, recipients, subject, saved_to_sent, sent_at)` to a local `sent_log` table. A retry with the same key (e.g. after a network blip) returns the original result without contacting SMTP again -- no duplicate sends.

## Installation

### As an MCP server for Claude Code

```bash
# Install from local checkout
pipx install /path/to/IMAP-MCP

# Or install from GitHub
pipx install git+https://github.com/newlc/IMAP-mcp.git

# Store your IMAP password securely in the OS keyring
# (this will verify the connection before saving)
imap-mcp --set-password --config /path/to/config.json

# Add to Claude Code (project-level, current project only)
claude mcp add imap-mcp /path/to/imap-mcp -- --config /path/to/config.json

# Add to Claude Code (global, available in all projects)
claude mcp add --scope user imap-mcp /path/to/imap-mcp -- --config /path/to/config.json
```

### For Claude Desktop

Add the following to your `claude_desktop_config.json` (typically at `~/Library/Application Support/Claude/claude_desktop_config.json` on macOS):

```json
{
  "mcpServers": {
    "imap-mcp": {
      "command": "imap-mcp",
      "args": ["--config", "/path/to/config.json"]
    }
  }
}
```

If you installed with `pipx`, use the full path to the binary:

```json
{
  "mcpServers": {
    "imap-mcp": {
      "command": "/Users/yourname/.local/bin/imap-mcp",
      "args": ["--config", "/path/to/config.json"]
    }
  }
}
```

### For VS Code (Copilot / Cline / Continue)

Add to your `.vscode/mcp.json` (or create it):

```json
{
  "servers": {
    "imap-mcp": {
      "command": "imap-mcp",
      "args": ["--config", "/path/to/config.json"]
    }
  }
}
```

If installed via `pipx`, use the full path:

```json
{
  "servers": {
    "imap-mcp": {
      "command": "/Users/yourname/.local/bin/imap-mcp",
      "args": ["--config", "/path/to/config.json"]
    }
  }
}
```

### For Cursor

Add to your `.cursor/mcp.json` in the project root (or `~/.cursor/mcp.json` for global):

```json
{
  "mcpServers": {
    "imap-mcp": {
      "command": "imap-mcp",
      "args": ["--config", "/path/to/config.json"]
    }
  }
}
```

With `pipx`:

```json
{
  "mcpServers": {
    "imap-mcp": {
      "command": "/Users/yourname/.local/bin/imap-mcp",
      "args": ["--config", "/path/to/config.json"]
    }
  }
}
```

### For Windsurf

Add to `~/.codeium/windsurf/mcp_config.json`:

```json
{
  "mcpServers": {
    "imap-mcp": {
      "command": "imap-mcp",
      "args": ["--config", "/path/to/config.json"]
    }
  }
}
```

### CLAUDE.md / .cursorrules / AGENTS.md integration

Add a snippet to your project instructions file so the AI assistant knows how to use the email server:

```markdown
## Email

Use the `imap-mcp` MCP server to access email. Workflow:

1. Call `auto_connect` first (reads config.json and credentials from keyring).
2. Use `load_cache` with mode "recent" to populate the local cache.
3. Use `fetch_emails`, `search_emails`, `get_email` etc. to read emails.
   These will be served from cache when possible.
4. Use `move_email`, `archive_email`, `flag_email` to organize.
5. Use `save_draft` to compose replies (drafts appear in the mail client for review before sending).
6. Use `process_auto_archive` with `dry_run: true` to preview, then without to archive.
```

## Configuration

Copy `config.json.example` to `config.json` and edit:

```jsonc
{
  // IMAP server connection
  "imap": {
    "host": "imap.example.com",     // IMAP server hostname
    "port": 993,                     // IMAP port (993 = SSL/TLS)
    "secure": true                   // Use SSL/TLS
  },

  // SMTP server (used for saving drafts with proper headers)
  "smtp": {
    "host": "smtp.example.com",
    "port": 587
  },

  // Credentials -- leave password EMPTY and use --set-password
  "credentials": {
    "username": "your-email@example.com",
    "password": ""
  },

  // User identity (used in From header and signature for drafts)
  "user": {
    "name": "Your Name",
    "email": "your-email@example.com",
    "signature": {
      "enabled": true,
      "text": "\n\n--\nYour Name\nTel: +1 234 567890",
      "html": "<br><br><div style=\"color:#666\">--<br><b>Your Name</b><br>Tel: +1 234 567890</div>"
    }
  },

  // Mailbox folder names (adjust to match your IMAP server)
  "folders": {
    "inbox": "INBOX",
    "next": "Next",                  // GTD-style action folders
    "waiting": "Waiting",
    "someday": "Someday",
    "archive": "Archive",
    "drafts": "Drafts"
  },

  // In-memory cache for the IDLE watcher
  "cache": {
    "enabled": true,                 // Auto-start IDLE watcher on connect
    "ttl_seconds": 300               // Cache TTL for overview queries
  },

  // Persistent SQLite cache (add these fields to enable)
  // "cache": {
  //   "db_path": "~/.imap-mcp/cache.db",  // Path to SQLite database
  //   "encrypt": true                      // true = in-memory DB + encrypted file on disk
  //                                        // false = plain SQLite file (better for 100K+ emails)
  // },

  // Auto-archive configuration
  "auto_archive": {
    "enabled": true,
    "senders_file": "auto_archive_senders.json"  // JSON file with sender patterns
  }
}
```

### Storing your password

```bash
imap-mcp --set-password --config /path/to/config.json
```

This will:
1. Read the username from `config.json`
2. Prompt for the password
3. Test the IMAP connection
4. Store the password in the OS keyring on success

To remove a stored password:

```bash
imap-mcp --delete-password --config /path/to/config.json
```

## Multiple accounts

Each email account requires its own `config.json` and its own MCP server instance. Example with two accounts:

```bash
# Store passwords
imap-mcp --set-password --config ~/mail/work-config.json
imap-mcp --set-password --config ~/mail/personal-config.json

# Add both to Claude Code
claude mcp add imap-work   /path/to/imap-mcp -- --config ~/mail/work-config.json
claude mcp add imap-personal /path/to/imap-mcp -- --config ~/mail/personal-config.json
```

Claude will see both as separate MCP servers and can access either account by name.

## Cache modes (`load_cache` tool)

The `load_cache` tool downloads emails (with bodies and attachments) into the local SQLite cache for offline access. It supports four modes:

| Mode | Description | Key parameters |
|------|-------------|----------------|
| `recent` | Load the last N emails (newest first) | `count` (default: 100) |
| `new` | Only emails newer than the most recent cached email | -- |
| `older` | Go further back in time: N emails older than the oldest cached | `count` (default: 100) |
| `range` | Emails within a specific date range | `since`, `before` (ISO dates) |

All modes are **incremental** -- emails already in the cache are skipped.

### Examples

```
# Load the 200 most recent emails
load_cache(mailbox="INBOX", mode="recent", count=200)

# Check for new emails since last cache load
load_cache(mailbox="INBOX", mode="new")

# Go further back: load 100 older emails
load_cache(mailbox="INBOX", mode="older", count=100)

# Load emails from January 2026
load_cache(mailbox="INBOX", mode="range", since="2026-01-01", before="2026-02-01")
```

Use `get_cache_stats` to see how many emails are cached, database size, and encryption status.

## Available tools

### Connection (4 tools)

| Tool | Description |
|------|-------------|
| `auto_connect` | Connect using config.json + keyring credentials (recommended) |
| `connect` | Manual IMAP connection to a host |
| `authenticate` | Manual login with username/password |
| `disconnect` | Close connection and stop watchers |

### Mailbox management (7 tools, +3 destructive under --write)

| Tool | Description |
|------|-------------|
| `list_mailboxes` | List all mailbox folders |
| `select_mailbox` | Open a mailbox folder |
| `create_mailbox` | Create a new folder |
| `get_mailbox_status` | Get message count, unseen, UIDNEXT, etc. |
| `subscribe_mailbox` | Add mailbox to subscribed list (LSUB) |
| `unsubscribe_mailbox` | Remove mailbox from subscribed list |
| `list_subscribed_mailboxes` | List subscribed mailboxes |
| `rename_mailbox` *(--write)* | Rename a folder |
| `delete_mailbox` *(--write)* | Delete a folder |
| `empty_mailbox` *(--write)* | Wipe every message in a folder (`\Deleted` + `EXPUNGE`) |

### Email reading (10 tools)

| Tool | Description |
|------|-------------|
| `fetch_emails` | Fetch emails with limit/offset, date filters |
| `get_email` | Get complete email (headers + body + attachments) by UID |
| `get_email_headers` | Get headers only (faster) |
| `get_email_body` | Get body as text or HTML (raw) |
| `get_email_body_safe` | Sanitized HTML (bleach) with optional remote-image / link stripping; cid: inline images replaced with data: URIs |
| `get_email_summary` | Compact AI-friendly summary list for many UIDs in one call |
| `get_calendar_invites` | Parse text/calendar parts (method, organizer, attendees, start/end) |
| `get_attachments` | List attachment metadata |
| `download_attachment` | Download attachment content (base64) |
| `get_thread` | Get email conversation thread |

### Search (9 tools)

| Tool | Description |
|------|-------------|
| `search_emails` | Free-text or IMAP SEARCH syntax |
| `search_by_sender` | Search by sender address |
| `search_by_subject` | Search by subject text |
| `search_by_date` | Search by date range |
| `search_unread` | Get all unread emails |
| `search_flagged` | Get all flagged/starred emails |
| `search_advanced` | Combined query: sender/recipient/subject/date/has_attachments/unread/flagged in one call (server-side IMAP SEARCH or local FTS) |
| `search_emails_fts` | Full-text search over the local SQLite FTS5 index (subject, body, addresses) |
| `rebuild_search_index` | Rebuild the FTS5 index from cached email bodies |

### Actions (12 tools)

| Tool | Description |
|------|-------------|
| `mark_read` | Mark emails as read |
| `mark_unread` | Mark emails as unread |
| `flag_email` | Add a flag (e.g. `\Flagged`, `\Important`) |
| `unflag_email` | Remove a flag |
| `move_email` | Move emails to another folder |
| `copy_email` | Copy emails to another folder |
| `archive_email` | Move emails to Archive folder |
| `save_draft` | Save a draft with optional signature and file attachments |
| `update_draft` | Replace a draft (APPEND new + EXPUNGE old; returns new UID) |
| `delete_draft` | Permanently delete one draft from the Drafts folder |
| `report_spam` | Move to Spam folder + add `$Junk` keyword (trains server-side filters) |
| `mark_not_spam` | Move out of Spam, clear `$Junk`, set `$NotJunk` |
| `bulk_action` | Apply one action (mark_read/flag/archive/move/copy/delete/report_spam) to every UID matching from/subject/date/unread/flagged criteria, with `dry_run` |

### Write-mode actions (4 tools, require `--write`)

| Tool | Description |
|------|-------------|
| `send_email` | Send via SMTP, optionally save copy to Sent (supports attachments) |
| `reply_email` | Reply / reply-all by UID, with proper `In-Reply-To`/`References` headers |
| `forward_email` | Forward by UID, re-attaches original attachments by default |
| `delete_email` | Move to Trash (default) or permanently delete with `\Deleted` + EXPUNGE |

### Statistics (2 tools)

| Tool | Description |
|------|-------------|
| `get_unread_count` | Unread email count |
| `get_total_count` | Total email count in mailbox |

### Cache and watch (5 tools)

| Tool | Description |
|------|-------------|
| `get_cached_overview` | Get in-memory overview of inbox/next/waiting/someday |
| `refresh_cache` | Force refresh the in-memory cache |
| `start_watch` | Start IDLE watchers on all configured folders |
| `stop_watch` | Stop all IDLE watchers |
| `idle_watch` | Watch a single mailbox temporarily (with timeout) |

### Persistent cache (5 tools, +1 under --write)

| Tool | Description |
|------|-------------|
| `sync_emails` | Download emails for a date range into SQLite (incremental) |
| `load_cache` | Flexible cache loader (recent/new/older/range modes) |
| `get_cache_stats` | Cache statistics (count, size, encryption status) |
| `cleanup_sent_log` | Purge `sent_log` rows older than N days (default: 30) |
| `vacuum_cache` | `VACUUM` + FTS5 `optimize` to compact the database |
| `rotate_encryption_key` *(--write)* | Re-encrypt the on-disk snapshot with a fresh Fernet key; previous key backed up under `<keyring_username>.previous` |

### Auto-archive (5 tools)

| Tool | Description |
|------|-------------|
| `get_auto_archive_list` | List auto-archive sender patterns |
| `add_auto_archive_sender` | Add a sender/domain to auto-archive |
| `remove_auto_archive_sender` | Remove a sender from auto-archive |
| `reload_auto_archive` | Reload sender list from file |
| `process_auto_archive` | Archive matching emails (supports dry_run) |

### Server metadata (4 tools)

| Tool | Description |
|------|-------------|
| `get_capabilities` | IMAP `CAPABILITY` (e.g. `IDLE`, `THREAD=REFERENCES`, `QUOTA`) |
| `get_namespace` | IMAP `NAMESPACE` (personal/other/shared prefixes) |
| `get_quota` | IMAP `QUOTA` usage for a mailbox |
| `get_server_id` | IMAP `ID` server info (RFC 2971) |
| `accounts_health` *(global)* | Per-account NOOP + cache/watcher status (read-only, no new connections) |

### MCP resources & prompts

In addition to tools, the server exposes the standard MCP `resources` and `prompts` surfaces.

**Resources** (clients can request them with `resources/read`):

| URI | Returns |
|-----|---------|
| `imap://{account}/overview` | Markdown summary of unread INBOX |
| `imap://{account}/health` | JSON health snapshot for the account |
| `imap://{account}/{mailbox}/{uid}` | Single email rendered as markdown (template) |
| `imap://{account}/{mailbox}/summary` | 20-email recent summary as markdown (template) |

**Prompts** (clients can offer them via `prompts/list` + `prompts/get`):

| Name | Purpose |
|------|---------|
| `summarize_inbox` | Concise digest of unread emails |
| `triage_inbox` | Classify into action / waiting / archive (with `dry_run` move proposal) |
| `draft_reply` | Draft (don't send) a reply to a specific UID |
| `extract_action_items` | Pull explicit/implicit action items from one email |
| `find_similar_emails` | FTS5 search + summary for a topic |

### Sieve / server-side rules (3 read tools, +3 under --write)

ManageSieve (RFC 5804) tools require `sieve.host` configured for the account. Server connections are short-lived (one operation per connection).

| Tool | Description |
|------|-------------|
| `sieve_list_scripts` | List all Sieve scripts and which one is active |
| `sieve_get_script` | Fetch the source of a script by name |
| `sieve_check_script` | Validate a script against the server (`CHECKSCRIPT`) |
| `sieve_put_script` *(--write)* | Upload (create/replace) a script |
| `sieve_delete_script` *(--write)* | Delete a script |
| `sieve_activate_script` *(--write)* | Activate a script (or pass empty name to deactivate all) |

## Requirements

- Python 3.10+
- Dependencies (installed automatically):
  - `imapclient` >= 3.0.0 -- IMAP protocol client
  - `mcp` >= 1.0.0 -- Model Context Protocol SDK
  - `pydantic` >= 2.0.0 -- data validation
  - `keyring` >= 25.0.0 -- OS keyring integration
  - `cryptography` >= 43.0.0 -- AES encryption for cache

## License

MIT
