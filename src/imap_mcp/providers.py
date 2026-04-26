"""Provider templates and config validation.

* :data:`PROVIDER_TEMPLATES` -- starter account blocks for popular email
  providers (Gmail, Outlook/Microsoft 365, Fastmail, Proton, iCloud, Yahoo).
  Used by ``imap-mcp --init-account <provider>``.
* :func:`validate_config` -- structural + best-effort connectivity check
  for ``config.json``. Used by ``imap-mcp --check-config``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional


# Map of well-known providers to their starter account blocks. Keep these
# minimal but production-correct; users layer their own user/credentials/
# folders/cache settings on top.
PROVIDER_TEMPLATES: dict[str, dict] = {
    "gmail": {
        "imap": {"host": "imap.gmail.com", "port": 993, "secure": True},
        "smtp": {"host": "smtp.gmail.com", "port": 465, "secure": True},
        # Gmail folders use the [Gmail]/* prefix when accessed via IMAP.
        "folders": {
            "inbox": "INBOX",
            "drafts": "[Gmail]/Drafts",
            "sent": "[Gmail]/Sent Mail",
            "trash": "[Gmail]/Trash",
            "spam": "[Gmail]/Spam",
            "archive": "[Gmail]/All Mail",
        },
        "_notes": (
            "Gmail requires an app password (account.google.com -> Security "
            "-> 2-Step Verification -> App passwords) OR OAuth2 (not yet "
            "supported by imap-mcp). Sieve / ManageSieve is not available."
        ),
    },
    "outlook": {
        "imap": {"host": "outlook.office365.com", "port": 993, "secure": True},
        "smtp": {"host": "smtp.office365.com", "port": 587, "starttls": True},
        "folders": {
            "inbox": "INBOX",
            "drafts": "Drafts",
            "sent": "Sent Items",
            "trash": "Deleted Items",
            "spam": "Junk Email",
            "archive": "Archive",
        },
        "_notes": (
            "Microsoft 365 / Outlook.com typically require OAuth2; basic "
            "auth was deprecated. Use an app password if your tenant still "
            "allows it, or wait for OAuth2 support."
        ),
    },
    "fastmail": {
        "imap": {"host": "imap.fastmail.com", "port": 993, "secure": True},
        "smtp": {"host": "smtp.fastmail.com", "port": 465, "secure": True},
        "sieve": {"host": "sieve.fastmail.com", "port": 4190, "starttls": True},
        "folders": {
            "inbox": "INBOX",
            "drafts": "Drafts",
            "sent": "Sent",
            "trash": "Trash",
            "spam": "Spam",
            "archive": "Archive",
        },
        "_notes": (
            "Fastmail supports app-specific passwords (Settings -> Privacy "
            "& Security -> Integrations -> 3rd Party Apps). ManageSieve is "
            "supported and pre-configured above."
        ),
    },
    "proton": {
        "imap": {"host": "127.0.0.1", "port": 1143, "secure": False, "starttls": True},
        "smtp": {"host": "127.0.0.1", "port": 1025, "starttls": True},
        "folders": {
            "inbox": "INBOX",
            "drafts": "Drafts",
            "sent": "Sent",
            "trash": "Trash",
            "spam": "Spam",
            "archive": "Archive",
        },
        "_notes": (
            "Proton Mail requires the Proton Mail Bridge to expose IMAP/SMTP "
            "over localhost. Install Bridge first; it generates the "
            "credentials and prints the localhost ports above."
        ),
    },
    "icloud": {
        "imap": {"host": "imap.mail.me.com", "port": 993, "secure": True},
        "smtp": {"host": "smtp.mail.me.com", "port": 587, "starttls": True},
        "folders": {
            "inbox": "INBOX",
            "drafts": "Drafts",
            "sent": "Sent Messages",
            "trash": "Deleted Messages",
            "spam": "Junk",
            "archive": "Archive",
        },
        "_notes": (
            "iCloud Mail requires an app-specific password "
            "(appleid.apple.com -> Sign-In and Security -> App-Specific Passwords)."
        ),
    },
    "yahoo": {
        "imap": {"host": "imap.mail.yahoo.com", "port": 993, "secure": True},
        "smtp": {"host": "smtp.mail.yahoo.com", "port": 465, "secure": True},
        "folders": {
            "inbox": "INBOX",
            "drafts": "Draft",
            "sent": "Sent",
            "trash": "Trash",
            "spam": "Bulk Mail",
            "archive": "Archive",
        },
        "_notes": (
            "Yahoo Mail requires an app password "
            "(login.yahoo.com -> Account security -> Generate app password)."
        ),
    },
    "yandex": {
        "imap": {"host": "imap.yandex.com", "port": 993, "secure": True},
        "smtp": {"host": "smtp.yandex.com", "port": 465, "secure": True},
        "folders": {
            "inbox": "INBOX",
            "drafts": "Drafts",
            "sent": "Sent",
            "trash": "Trash",
            "spam": "Spam",
            "archive": "Archive",
        },
        "_notes": (
            "Yandex Mail requires an app password "
            "(id.yandex.com -> Security -> App passwords). "
            "Enable IMAP first in Yandex Mail settings."
        ),
    },
}


def make_starter_account(
    provider: str,
    name: str,
    username: str,
    *,
    user_full_name: Optional[str] = None,
    default: bool = True,
    encrypt_cache: bool = False,
) -> dict:
    """Build a ready-to-edit account block for ``provider``."""
    if provider not in PROVIDER_TEMPLATES:
        raise ValueError(
            f"Unknown provider: {provider!r}. "
            f"Known: {sorted(PROVIDER_TEMPLATES)}"
        )
    base = json.loads(json.dumps(PROVIDER_TEMPLATES[provider]))  # deep copy
    notes = base.pop("_notes", None)
    account: dict = {
        "name": name,
        "default": default,
        **base,
        "credentials": {"username": username, "password": ""},
        "user": {"name": user_full_name or username, "email": username},
        "cache": {
            "enabled": True,
            "db_path": f"~/.imap-mcp/{name}.db",
            "encrypt": encrypt_cache,
        },
    }
    if notes:
        account["_notes"] = notes
    return account


# ---------------------------------------------------------------------------
# Config validation (structural + best-effort connectivity)
# ---------------------------------------------------------------------------


def validate_config(
    config_path: str,
    *,
    check_connection: bool = False,
    check_keyring: bool = True,
) -> dict:
    """Validate a config.json file.

    Returns ``{"valid": bool, "errors": [...], "warnings": [...],
    "accounts": [{name, ok, errors, warnings}]}``.

    With ``check_keyring=True`` (default) verifies that each account's
    password is reachable in the OS keyring. With
    ``check_connection=True`` actually opens an IMAP connection to test
    each account -- skip in CI.
    """
    errors: list[str] = []
    warnings: list[str] = []
    accounts_results: list[dict] = []

    path = Path(config_path).expanduser()
    if not path.exists():
        return {
            "valid": False,
            "errors": [f"Config file not found: {path}"],
            "warnings": [],
            "accounts": [],
        }
    try:
        with open(path) as f:
            raw = json.load(f)
    except json.JSONDecodeError as exc:
        return {
            "valid": False,
            "errors": [f"Malformed JSON: {exc}"],
            "warnings": [],
            "accounts": [],
        }

    from .accounts import AccountManager
    if "accounts" not in raw:
        if AccountManager.is_legacy_config(raw):
            errors.append(
                "Legacy single-account format. "
                "Run 'imap-mcp --migrate-config' first."
            )
        else:
            errors.append("'accounts' key is missing.")
        return {
            "valid": False, "errors": errors,
            "warnings": warnings, "accounts": [],
        }

    accounts_data = raw["accounts"]
    if not isinstance(accounts_data, list) or not accounts_data:
        errors.append("config.accounts must be a non-empty array.")
        return {
            "valid": False, "errors": errors,
            "warnings": warnings, "accounts": [],
        }

    seen_names: set[str] = set()
    defaults: list[str] = []
    for raw_account in accounts_data:
        result = _validate_one_account(
            raw_account, seen_names, defaults,
            check_connection=check_connection,
            check_keyring=check_keyring,
        )
        accounts_results.append(result)

    if len(defaults) > 1:
        errors.append(f"Multiple accounts marked default: {defaults}")
    if len(accounts_data) > 1 and not defaults:
        errors.append(
            "Multi-account config but no account marked 'default: true'."
        )

    valid = not errors and all(r["ok"] for r in accounts_results)
    return {
        "valid": valid,
        "errors": errors,
        "warnings": warnings,
        "accounts": accounts_results,
    }


def _validate_one_account(
    raw: dict,
    seen_names: set,
    defaults: list,
    *,
    check_connection: bool,
    check_keyring: bool,
) -> dict:
    errors: list[str] = []
    warnings: list[str] = []
    name = raw.get("name", "<unnamed>")
    if not raw.get("name"):
        errors.append("missing 'name' field")
    elif raw["name"] in seen_names:
        errors.append(f"duplicate account name: {raw['name']!r}")
    else:
        seen_names.add(raw["name"])

    if raw.get("default") is True:
        defaults.append(raw.get("name", "<unnamed>"))

    imap_cfg = raw.get("imap") or {}
    if not imap_cfg.get("host"):
        errors.append("imap.host is required")

    creds = raw.get("credentials") or {}
    username = creds.get("username", "")
    if not username:
        errors.append("credentials.username is required")

    cache_cfg = raw.get("cache") or {}
    if cache_cfg.get("encrypt") and not cache_cfg.get("enabled", True):
        warnings.append(
            "cache.encrypt=true with cache.enabled=false has no effect"
        )

    if check_keyring and username and not creds.get("password"):
        try:
            import keyring
            kr_password = keyring.get_password("imap-mcp", username)
            if not kr_password:
                warnings.append(
                    f"no password stored in keyring for {username!r}; run "
                    f"'imap-mcp --set-password --account {name}'"
                )
        except Exception as exc:
            warnings.append(f"keyring check failed: {exc}")

    if check_connection and not errors:
        try:
            from imapclient import IMAPClient
            host = imap_cfg["host"]
            port = imap_cfg.get("port", 993)
            secure = imap_cfg.get("secure", True)
            client = IMAPClient(host, port=port, ssl=secure)
            password = creds.get("password") or ""
            if not password:
                try:
                    import keyring
                    password = keyring.get_password("imap-mcp", username) or ""
                except Exception:
                    password = ""
            if password:
                client.login(username, password)
                client.logout()
            else:
                warnings.append(
                    "no password available; skipped IMAP login test"
                )
        except Exception as exc:
            errors.append(f"IMAP connection failed: {exc}")

    return {
        "name": name,
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# JSON Schema for config.json
# ---------------------------------------------------------------------------


CONFIG_JSON_SCHEMA: dict = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "imap-mcp config",
    "type": "object",
    "required": ["accounts"],
    "additionalProperties": True,
    "properties": {
        "accounts": {
            "type": "array",
            "minItems": 1,
            "items": {"$ref": "#/$defs/account"},
        },
        "auto_archive": {"$ref": "#/$defs/auto_archive"},
    },
    "$defs": {
        "account": {
            "type": "object",
            "required": ["name", "imap", "credentials"],
            "properties": {
                "name": {"type": "string", "minLength": 1},
                "default": {"type": "boolean"},
                "imap": {
                    "type": "object",
                    "required": ["host"],
                    "properties": {
                        "host": {"type": "string"},
                        "port": {"type": "integer", "default": 993},
                        "secure": {"type": "boolean", "default": True},
                    },
                },
                "smtp": {
                    "type": "object",
                    "properties": {
                        "host": {"type": "string"},
                        "port": {"type": "integer"},
                        "secure": {"type": "boolean"},
                        "starttls": {"type": "boolean"},
                    },
                },
                "sieve": {
                    "type": "object",
                    "properties": {
                        "host": {"type": "string"},
                        "port": {"type": "integer", "default": 4190},
                        "secure": {"type": "boolean"},
                        "starttls": {"type": "boolean"},
                    },
                },
                "credentials": {
                    "type": "object",
                    "required": ["username"],
                    "properties": {
                        "username": {"type": "string"},
                        "password": {
                            "type": "string",
                            "description": "Leave empty; use 'imap-mcp --set-password'.",
                        },
                    },
                },
                "user": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "email": {"type": "string"},
                        "signature": {
                            "type": "object",
                            "properties": {
                                "enabled": {"type": "boolean"},
                                "text": {"type": "string"},
                                "html": {"type": "string"},
                            },
                        },
                    },
                },
                "folders": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                },
                "cache": {
                    "type": "object",
                    "properties": {
                        "enabled": {"type": "boolean"},
                        "db_path": {"type": "string"},
                        "encrypt": {"type": "boolean"},
                        "ttl_seconds": {"type": "integer"},
                        "keyring_username": {"type": "string"},
                    },
                },
                "spam": {
                    "type": "object",
                    "properties": {
                        "junk_flag": {"type": "string"},
                        "not_junk_flag": {"type": "string"},
                    },
                },
                "security": {
                    "type": "object",
                    "properties": {
                        "max_attachment_size_mb": {"type": "number"},
                        "attachments_allowed_dirs": {
                            "type": "array", "items": {"type": "string"},
                        },
                    },
                },
                "auto_archive": {"$ref": "#/$defs/auto_archive"},
            },
        },
        "auto_archive": {
            "type": "object",
            "properties": {
                "enabled": {"type": "boolean"},
                "senders_file": {"type": "string"},
            },
        },
    },
}
