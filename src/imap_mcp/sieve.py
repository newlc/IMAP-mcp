"""Minimal ManageSieve (RFC 5804) client.

Implements just the subset needed for an MCP-style "manage your filters"
flow: AUTHENTICATE PLAIN, CAPABILITY, LISTSCRIPTS, GETSCRIPT, PUTSCRIPT,
DELETESCRIPT, SETACTIVE, CHECKSCRIPT, LOGOUT.

Connection is short-lived (one operation, one connection): we don't keep
sockets open between tool calls. This keeps state simple and avoids
connection-loss bugs across long agent sessions. ManageSieve is text-based
and fast to set up, so the overhead is negligible.
"""

from __future__ import annotations

import base64
import logging
import re
import socket
import ssl
from typing import Optional

logger = logging.getLogger(__name__)


class SieveError(Exception):
    """Raised when the ManageSieve server returns NO/BYE or the protocol fails."""


# Allow the trailing \r\n to be present or absent: callers usually strip the
# line before testing it against this pattern (literal indicators arrive on
# their own line, so the strip happens at _read_line level).
_LITERAL_RE = re.compile(rb"\{(\d+)\+?\}(?:\r\n)?")


class ManageSieveClient:
    """Tiny ManageSieve client for one short-lived session."""

    def __init__(
        self,
        host: str,
        port: int = 4190,
        secure: bool = False,
        starttls: bool = True,
        timeout: int = 30,
    ):
        self.host = host
        self.port = port
        self.secure = secure
        self.starttls = starttls
        self.timeout = timeout
        self.sock: Optional[socket.socket] = None
        self._buf: bytes = b""
        self.capabilities: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Low-level I/O
    # ------------------------------------------------------------------

    def _connect(self) -> None:
        raw = socket.create_connection((self.host, self.port), timeout=self.timeout)
        if self.secure:
            ctx = ssl.create_default_context()
            self.sock = ctx.wrap_socket(raw, server_hostname=self.host)
        else:
            self.sock = raw
        self._buf = b""
        self._read_capabilities()
        if not self.secure and self.starttls and "STARTTLS" in {
            k.upper() for k in self.capabilities
        }:
            self._send(b"STARTTLS\r\n")
            status, _ = self._read_response()
            if status != "OK":
                raise SieveError("STARTTLS rejected by server")
            ctx = ssl.create_default_context()
            self.sock = ctx.wrap_socket(self.sock, server_hostname=self.host)
            self._buf = b""
            self._read_capabilities()

    def _send(self, data: bytes) -> None:
        assert self.sock is not None
        self.sock.sendall(data)

    def _recv_chunk(self) -> bytes:
        assert self.sock is not None
        chunk = self.sock.recv(8192)
        if not chunk:
            raise SieveError("Connection closed by server")
        return chunk

    def _read_line(self) -> bytes:
        while b"\r\n" not in self._buf:
            self._buf += self._recv_chunk()
        line, _, rest = self._buf.partition(b"\r\n")
        self._buf = rest
        return line

    def _read_n(self, n: int) -> bytes:
        while len(self._buf) < n:
            self._buf += self._recv_chunk()
        data, self._buf = self._buf[:n], self._buf[n:]
        return data

    def _read_string_value(self, raw: bytes) -> str:
        """Decode a server-quoted/literal string into Python str."""
        s = raw.strip()
        m = _LITERAL_RE.fullmatch(s)
        if m:
            length = int(m.group(1))
            payload = self._read_n(length)
            self._read_line()  # trailing CRLF after literal
            return payload.decode("utf-8", errors="replace")
        if s.startswith(b'"') and s.endswith(b'"'):
            return s[1:-1].decode("utf-8", errors="replace").replace('\\"', '"')
        return s.decode("utf-8", errors="replace")

    def _read_capabilities(self) -> None:
        """Read the server greeting until OK / NO / BYE arrives."""
        self.capabilities = {}
        while True:
            line = self._read_line()
            upper = line.upper().strip()
            if upper.startswith(b"OK"):
                return
            if upper.startswith(b"NO") or upper.startswith(b"BYE"):
                raise SieveError(line.decode("utf-8", errors="replace"))
            # Capability lines look like:
            #   "IMPLEMENTATION" "Dovecot Pigeonhole"
            #   "SASL" "PLAIN LOGIN"
            #   "STARTTLS"
            #   "VERSION" "1.0"
            parts = self._tokenize(line)
            if not parts:
                continue
            key = parts[0]
            value = parts[1] if len(parts) > 1 else ""
            self.capabilities[key.upper()] = value

    @staticmethod
    def _tokenize(line: bytes) -> list[str]:
        """Tokenize a capability/list line into bare strings."""
        out: list[str] = []
        s = line.decode("utf-8", errors="replace").strip()
        i = 0
        while i < len(s):
            if s[i] == '"':
                end = s.find('"', i + 1)
                if end < 0:
                    out.append(s[i + 1:])
                    break
                out.append(s[i + 1: end].replace('\\"', '"'))
                i = end + 1
            elif s[i].isspace():
                i += 1
            else:
                j = i
                while j < len(s) and not s[j].isspace():
                    j += 1
                out.append(s[i:j])
                i = j
        return out

    def _read_response(self) -> tuple[str, list[bytes]]:
        """Read response lines until the final OK/NO/BYE.

        Returns the final status word and any preceding data lines (raw).
        """
        data_lines: list[bytes] = []
        while True:
            line = self._read_line()
            upper = line.upper().strip()
            if upper.startswith(b"OK"):
                return "OK", data_lines
            if upper.startswith(b"NO") or upper.startswith(b"BYE"):
                detail = line.decode("utf-8", errors="replace").strip()
                raise SieveError(detail)
            data_lines.append(line)

    # ------------------------------------------------------------------
    # High-level
    # ------------------------------------------------------------------

    @staticmethod
    def _quote(s: str) -> bytes:
        """Quote a string for ManageSieve. Uses literals for \\r\\n / quotes."""
        if "\r" in s or "\n" in s or '"' in s or "{" in s or len(s) > 1024:
            payload = s.encode("utf-8")
            return f"{{{len(payload)}+}}\r\n".encode("ascii") + payload
        return ('"' + s + '"').encode("utf-8")

    def login(self, username: str, password: str) -> None:
        token = b"\x00" + username.encode("utf-8") + b"\x00" + password.encode("utf-8")
        b64 = base64.b64encode(token)
        cmd = b"AUTHENTICATE \"PLAIN\" {" + str(len(b64)).encode() + b"+}\r\n" + b64 + b"\r\n"
        self._send(cmd)
        self._read_response()

    def logout(self) -> None:
        try:
            self._send(b"LOGOUT\r\n")
            try:
                self._read_response()
            except SieveError:
                pass
        finally:
            if self.sock:
                try:
                    self.sock.close()
                except Exception:
                    pass
            self.sock = None

    def __enter__(self):
        self._connect()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.logout()

    def listscripts(self) -> list[dict]:
        self._send(b"LISTSCRIPTS\r\n")
        _, lines = self._read_response()
        scripts: list[dict] = []
        for raw in lines:
            tokens = self._tokenize(raw)
            if not tokens:
                continue
            scripts.append({
                "name": tokens[0],
                "active": (len(tokens) > 1 and tokens[1].upper() == "ACTIVE"),
            })
        return scripts

    def getscript(self, name: str) -> str:
        self._send(b"GETSCRIPT " + self._quote(name) + b"\r\n")
        _, lines = self._read_response()
        if not lines:
            return ""
        # The first line contains a literal {N+}, and subsequent lines are
        # the script body. Reassemble.
        first = lines[0]
        m = _LITERAL_RE.fullmatch(first.strip())
        if m:
            # Literal already pulled into self._buf in _read_line ordering --
            # but here we received remainder as data lines. Concatenate the rest.
            remainder = b"\r\n".join(lines[1:])
            length = int(m.group(1))
            return remainder[:length].decode("utf-8", errors="replace")
        # Quoted form (rare).
        return self._read_string_value(first)

    def putscript(self, name: str, content: str) -> None:
        cmd = (
            b"PUTSCRIPT "
            + self._quote(name)
            + b" "
            + self._quote(content)
            + b"\r\n"
        )
        self._send(cmd)
        self._read_response()

    def deletescript(self, name: str) -> None:
        self._send(b"DELETESCRIPT " + self._quote(name) + b"\r\n")
        self._read_response()

    def setactive(self, name: str) -> None:
        self._send(b"SETACTIVE " + self._quote(name) + b"\r\n")
        self._read_response()

    def checkscript(self, content: str) -> dict:
        self._send(b"CHECKSCRIPT " + self._quote(content) + b"\r\n")
        try:
            self._read_response()
            return {"valid": True}
        except SieveError as exc:
            return {"valid": False, "error": str(exc)}


def open_for(account_config: dict, action: str = "use") -> ManageSieveClient:
    """Construct and connect a ``ManageSieveClient`` for an account.

    Reads ``sieve.host``, ``sieve.port`` (default 4190), ``sieve.secure``
    (default False / port 4190 = STARTTLS), ``sieve.starttls`` (default True).
    Authenticates using the account's IMAP credentials.
    """
    sieve_cfg = account_config.get("sieve", {})
    host = sieve_cfg.get("host")
    if not host:
        raise SieveError(
            f"Sieve not configured: add 'sieve.host' to account config to {action} scripts."
        )
    port = int(sieve_cfg.get("port", 4190))
    secure = bool(sieve_cfg.get("secure", port == 5190))
    starttls = bool(sieve_cfg.get("starttls", not secure))

    creds = account_config.get("credentials", {})
    username = creds.get("username", "")
    password = creds.get("password", "")
    if not password and username:
        # Resolve from keyring lazily to avoid circular imports.
        from .imap_client import get_stored_password
        password = get_stored_password(username) or ""
    if not username or not password:
        raise SieveError("No usable Sieve credentials (username/password).")

    client = ManageSieveClient(host=host, port=port, secure=secure, starttls=starttls)
    client._connect()
    client.login(username, password)
    return client
