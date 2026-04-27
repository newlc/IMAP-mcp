"""SendingMixin: SMTP send + reply + forward + draft CRUD + delete.

Pulled out of :mod:`imap_mcp.imap_client` to keep that module under
3000 lines. The mixin assumes the consuming class provides:

* ``self.client``           -- live IMAPClient (or None when disconnected)
* ``self.config``            -- per-account config dict
* ``self.email_cache``       -- :class:`~imap_mcp.cache.EmailCache` or None
* ``self.current_mailbox``   -- str
* ``self._ensure_connected()``
* ``self.select_mailbox(name)``
* ``self._extract_body(msg)``
* ``self._extract_attachment_info(msg)``
* ``self._get_attachment_bytes(msg, idx)``
* ``self._decode_header(value)``

This is a vanilla mixin -- no ``__init__``, no state of its own. State
lives on the consuming :class:`ImapClientWrapper`.
"""

from __future__ import annotations

import email
import email.utils
import logging
import mimetypes
import smtplib
import ssl
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional

from .mail_utils import with_retries

logger_imap = logging.getLogger("imap_mcp.imap_client")


def get_stored_password(username: str):
    """Indirection so tests can monkey-patch
    ``imap_mcp.imap_client.get_stored_password`` and have the SMTP path
    pick it up. The single source of truth lives in
    :mod:`imap_mcp.imap_client`; we re-resolve at call time.
    """
    from . import imap_client  # local import to avoid circularity at load time
    return imap_client.get_stored_password(username)


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


class SendingMixin:
    """Methods that produce/send/replace outgoing email."""

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
        inline_attachments: Optional[list[tuple[str, str, bytes]]] = None,
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

        if attachments or inline_attachments:
            if attachments:
                self._validate_attachment_paths(attachments)
            msg = MIMEMultipart("mixed")
            msg.attach(body_part)
            for path_str in attachments or []:
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
            # In-memory attachments -- bypass tempfile/disk roundtrip.
            # Each entry is (filename, content_type, raw_bytes).
            for filename, ctype, raw in inline_attachments or []:
                if not ctype:
                    ctype = "application/octet-stream"
                maintype, subtype = ctype.split("/", 1)
                part = MIMEBase(maintype, subtype)
                part.set_payload(raw)
                encoders.encode_base64(part)
                safe_name = (filename or "attachment").replace("/", "_")
                part.add_header(
                    "Content-Disposition", "attachment", filename=safe_name
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
        inline_attachments: Optional[list[tuple[str, str, bytes]]] = None,
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
            inline_attachments=inline_attachments,
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

        # Stream attachments directly from the original message into the
        # outgoing one -- no temp files, no double-encode round trip.
        inline_atts: list[tuple[str, str, bytes]] = []
        if include_attachments:
            for att in self._extract_attachment_info(orig):
                data_bytes = self._get_attachment_bytes(orig, att.index)
                if not data_bytes:
                    continue
                inline_atts.append((
                    att.filename or f"attachment_{att.index}",
                    att.content_type or "application/octet-stream",
                    data_bytes,
                ))

        return self.send_email(
            to=to,
            subject=subject,
            body=final_body,
            cc=cc,
            bcc=bcc,
            html_body=final_html,
            attachments=extra_attachments or None,
            inline_attachments=inline_atts or None,
            include_signature=include_signature,
            save_to_sent=save_to_sent,
            idempotency_key=idempotency_key,
        )

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
