"""Email content utilities: HTML sanitization, inline-image extraction,
calendar invite parsing, and a small retry-with-backoff helper.

Kept separate from :mod:`imap_mcp.imap_client` so each piece is independently
testable without an IMAP connection.
"""

from __future__ import annotations

import base64
import email
import functools
import logging
import time
from datetime import datetime
from typing import Any, Callable, Optional

import bleach
import html2text
from bleach.css_sanitizer import CSSSanitizer

logger = logging.getLogger(__name__)


def parse_authentication_results(headers) -> dict:
    """Parse one or more ``Authentication-Results`` headers (RFC 8601).

    Accepts either a single header value (str) or an iterable. Returns
    ``{"spf": "pass", "dkim": "fail", "dmarc": "pass", "raw": [...]}``
    where each verdict is the lowercased ``method=result`` token. Methods
    not present in the header are omitted (callers can treat absence as
    "unknown").
    """
    if isinstance(headers, str):
        values = [headers] if headers else []
    elif headers is None:
        values = []
    else:
        values = [v for v in headers if v]

    if not values:
        return {"raw": []}

    out: dict = {"raw": list(values)}
    # The header is a semicolon-separated list of methods. We're only after
    # the verdict tokens like spf=pass / dkim=fail / dmarc=pass.
    import re
    pattern = re.compile(
        r"\b(spf|dkim|dmarc|arc|bimi|dkim-atps)\s*=\s*([a-z]+)",
        re.IGNORECASE,
    )
    for value in values:
        for method, verdict in pattern.findall(value):
            method_l = method.lower()
            verdict_l = verdict.lower()
            # Keep the strongest negative signal we've seen (prefer "fail"
            # over "pass" if any header reports a failure).
            existing = out.get(method_l)
            if existing == "fail":
                continue
            if existing is None or verdict_l == "fail":
                out[method_l] = verdict_l
    return out


def html_to_plain(html: str) -> str:
    """Convert an HTML body to readable plain text.

    Used as a fallback when an email is HTML-only (no ``text/plain`` part)
    so the FTS index, snippet generator and ``get_email_summary`` still
    have searchable/displayable text.
    """
    if not html:
        return ""
    h = html2text.HTML2Text()
    h.ignore_links = False
    h.ignore_images = True   # data URIs would bloat the snippet
    h.ignore_emphasis = False
    h.body_width = 0          # don't wrap; let the consumer decide
    h.unicode_snob = True
    try:
        return h.handle(html).strip()
    except Exception as exc:
        logger.debug("html2text failed: %s", exc)
        # Last-ditch: strip tags via bleach.
        return bleach.clean(html, tags=[], strip=True).strip()


# ---------------------------------------------------------------------------
# HTML sanitization
# ---------------------------------------------------------------------------

# Conservative whitelist: every tag/attribute that's reasonable inside an
# email body. Anything else (script, style, iframe, embed, object, form,
# input, meta, link, base...) is dropped.
_SAFE_TAGS = frozenset({
    "a", "abbr", "address", "article", "aside", "b", "blockquote", "br",
    "caption", "cite", "code", "col", "colgroup", "dd", "del", "details",
    "dfn", "div", "dl", "dt", "em", "figcaption", "figure", "footer", "h1",
    "h2", "h3", "h4", "h5", "h6", "header", "hr", "i", "img", "ins", "kbd",
    "li", "mark", "ol", "p", "pre", "q", "s", "samp", "section", "small",
    "span", "strong", "sub", "summary", "sup", "table", "tbody", "td", "tfoot",
    "th", "thead", "time", "tr", "u", "ul", "var", "wbr",
})

_SAFE_ATTRS = {
    "*": ["class", "id", "title", "lang", "dir", "style"],
    "a": ["href", "name", "target", "rel"],
    "img": ["src", "alt", "title", "width", "height"],
    "td": ["colspan", "rowspan", "align", "valign"],
    "th": ["colspan", "rowspan", "align", "valign", "scope"],
    "table": ["border", "cellpadding", "cellspacing", "summary"],
    "col": ["span", "width"],
    "colgroup": ["span", "width"],
    "time": ["datetime"],
    "blockquote": ["cite"],
}

# CSS properties allowed inside style="...". Anything else (position, behavior,
# expression, url(...) etc.) is stripped by bleach's CSS sanitizer.
_SAFE_CSS_PROPS = frozenset({
    "color", "background-color", "background", "font-family", "font-size",
    "font-weight", "font-style", "text-align", "text-decoration", "border",
    "border-color", "border-style", "border-width", "border-left",
    "border-right", "border-top", "border-bottom", "border-radius", "padding",
    "padding-left", "padding-right", "padding-top", "padding-bottom",
    "margin", "margin-left", "margin-right", "margin-top", "margin-bottom",
    "width", "height", "max-width", "max-height", "min-width", "min-height",
    "display", "vertical-align", "line-height", "letter-spacing", "white-space",
})

_SAFE_PROTOCOLS = frozenset({"http", "https", "mailto", "cid", "data"})

_CSS_SANITIZER = CSSSanitizer(allowed_css_properties=list(_SAFE_CSS_PROPS))


import re as _re

# Tags whose entire body must be discarded -- bleach's ``strip=True`` would
# otherwise keep the inner text (e.g. ``<script>alert(1)</script>`` would
# leak ``alert(1)`` into the cleaned output).
_DROP_BLOCK_TAGS = ("script", "style", "noscript", "iframe", "object", "embed")
_DROP_BLOCK_RE = _re.compile(
    r"<\s*(" + "|".join(_DROP_BLOCK_TAGS) + r")\b[^>]*>.*?<\s*/\s*\1\s*>",
    _re.IGNORECASE | _re.DOTALL,
)


def sanitize_html(
    html: str,
    *,
    strip_remote_images: bool = False,
    strip_links: bool = False,
) -> str:
    """Return a sanitized version of ``html`` safe to render.

    * Removes ``<script>``, ``<style>``, ``<iframe>``, ``<object>``,
      ``<embed>``, ``<form>``, ``<input>``, ``<meta>``, ``<link>``,
      ``<base>`` and any tag not in :data:`_SAFE_TAGS`. The contents of
      ``<script>`` and ``<style>`` blocks are dropped entirely.
    * Strips event-handler attributes (``onclick``, ``onload`` ...) and any
      attribute not in :data:`_SAFE_ATTRS`.
    * Restricts ``href`` and ``src`` to safe URL schemes (``http``, ``https``,
      ``mailto``, ``cid``, ``data``). ``javascript:`` and friends are dropped.
    * Sanitizes ``style="..."`` via bleach's CSS sanitizer.

    With ``strip_remote_images=True``, removes any ``<img>`` whose ``src`` is
    not a ``cid:`` reference -- useful for blocking tracking pixels in
    untrusted mail.

    With ``strip_links=True``, replaces ``<a href>`` targets with ``#`` so
    nothing in a malicious email can lure the user into clicking through.
    """
    if not html:
        return ""

    # Pre-strip dangerous block tags *with* their content. Done before bleach
    # because bleach's strip=True would keep the inner text otherwise.
    pre = _DROP_BLOCK_RE.sub("", html)

    cleaned = bleach.clean(
        pre,
        tags=list(_SAFE_TAGS),
        attributes=_SAFE_ATTRS,
        protocols=list(_SAFE_PROTOCOLS),
        css_sanitizer=_CSS_SANITIZER,
        strip=True,
        strip_comments=True,
    )

    if strip_remote_images or strip_links:
        # Re-parse and rewrite specific attributes; bleach.clean already
        # removed dangerous tags so this second pass is just a regex on the
        # sanitized output -- safe and cheap.
        import re
        if strip_remote_images:
            def _kill_remote(match):
                src = match.group(1)
                return "" if not src.startswith("cid:") else match.group(0)
            cleaned = re.sub(
                r'<img[^>]*\ssrc="([^"]*)"[^>]*>',
                _kill_remote,
                cleaned,
                flags=re.IGNORECASE,
            )
        if strip_links:
            cleaned = re.sub(
                r'(<a[^>]*\s)href="[^"]*"',
                r'\1href="#"',
                cleaned,
                flags=re.IGNORECASE,
            )
    return cleaned


# ---------------------------------------------------------------------------
# Inline images (cid: references)
# ---------------------------------------------------------------------------


def extract_inline_images(msg: email.message.Message) -> list[dict]:
    """Return a list of ``{"cid", "filename", "content_type", "data_uri"}``.

    The ``Content-ID`` header is matched against ``cid:`` references in the
    HTML body; ``data_uri`` is a fully-baked ``data:<ct>;base64,<...>`` URL
    that can be inlined directly.
    """
    inline = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        cid = part.get("Content-ID") or ""
        if not cid:
            continue
        cid = cid.strip("<>").strip()
        ctype = part.get_content_type() or "application/octet-stream"
        # Only inline things that look like images (or anything if explicitly
        # marked Content-Disposition: inline).
        disp = (part.get("Content-Disposition") or "").lower()
        if not (ctype.startswith("image/") or "inline" in disp):
            continue
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        b64 = base64.b64encode(payload).decode("ascii")
        inline.append({
            "cid": cid,
            "filename": part.get_filename() or "",
            "content_type": ctype,
            "data_uri": f"data:{ctype};base64,{b64}",
        })
    return inline


def inline_cid_to_data_uri(html: str, inline_images: list[dict]) -> str:
    """Replace ``src="cid:..."`` in ``html`` with the matching data URI."""
    if not html or not inline_images:
        return html
    by_cid = {img["cid"]: img["data_uri"] for img in inline_images}
    import re

    def _sub(match):
        cid = match.group(1)
        return f'src="{by_cid[cid]}"' if cid in by_cid else match.group(0)

    return re.sub(r'src="cid:([^"]+)"', _sub, html, flags=re.IGNORECASE)


# ---------------------------------------------------------------------------
# Calendar invites (RFC 5545)
# ---------------------------------------------------------------------------


def parse_calendar_invites(msg: email.message.Message) -> list[dict]:
    """Return one dict per VEVENT found in a ``text/calendar`` part.

    Each dict contains ``method`` (REQUEST/REPLY/CANCEL), ``uid``,
    ``summary``, ``description``, ``location``, ``start`` / ``end`` (ISO
    timestamps), ``organizer``, ``attendees`` (list of dicts with email and
    partstat), and ``sequence``.
    """
    from icalendar import Calendar

    invites: list[dict] = []
    for part in msg.walk():
        ctype = (part.get_content_type() or "").lower()
        if ctype != "text/calendar":
            continue
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        try:
            cal = Calendar.from_ical(payload)
        except Exception as exc:
            logger.debug("Failed to parse calendar part: %s", exc)
            continue

        method = str(cal.get("METHOD", "")).upper() or None
        for component in cal.walk():
            if component.name != "VEVENT":
                continue
            invites.append(_serialize_vevent(component, method))
    return invites


def _serialize_vevent(component, method: Optional[str]) -> dict:
    """Convert an ``icalendar.Event`` component into a JSON-friendly dict."""

    def _val(field):
        v = component.get(field)
        if v is None:
            return None
        if hasattr(v, "dt"):
            dt = v.dt
            if isinstance(dt, datetime):
                return dt.isoformat()
            return str(dt)
        return str(v)

    def _addr(prop):
        if prop is None:
            return None
        return str(prop).replace("MAILTO:", "").replace("mailto:", "").strip()

    organizer = component.get("ORGANIZER")
    attendees_raw = component.get("ATTENDEE")
    if attendees_raw is None:
        attendees_list = []
    elif not isinstance(attendees_raw, list):
        attendees_list = [attendees_raw]
    else:
        attendees_list = attendees_raw

    attendees = []
    for a in attendees_list:
        email_addr = _addr(a)
        params = getattr(a, "params", {}) or {}
        attendees.append({
            "email": email_addr,
            "name": params.get("CN"),
            "partstat": params.get("PARTSTAT"),
            "role": params.get("ROLE"),
        })

    return {
        "method": method,
        "uid": _val("UID"),
        "summary": _val("SUMMARY"),
        "description": _val("DESCRIPTION"),
        "location": _val("LOCATION"),
        "start": _val("DTSTART"),
        "end": _val("DTEND"),
        "organizer": _addr(organizer),
        "attendees": attendees,
        "sequence": _val("SEQUENCE"),
        "status": _val("STATUS"),
    }


# ---------------------------------------------------------------------------
# Retry with exponential backoff
# ---------------------------------------------------------------------------


# Errors that are typically transient: socket-level, IMAP/SMTP timeouts and
# the generic IMAPClient OSError wrappers. We deliberately do *not* retry on
# authentication failures or NO/BAD responses -- those don't recover by
# trying again.
def _is_transient(exc: BaseException) -> bool:
    if isinstance(exc, (TimeoutError, ConnectionError, BrokenPipeError, OSError)):
        return True
    name = type(exc).__name__.lower()
    if "timeout" in name or "broken" in name or "abort" in name:
        return True
    msg = str(exc).lower()
    return any(t in msg for t in (
        "timed out", "timeout", "connection reset", "connection aborted",
        "broken pipe", "eof occurred", "temporarily unavailable",
        "try again", "service unavailable",
    ))


def with_retries(
    fn: Callable,
    *,
    attempts: int = 3,
    base_delay: float = 0.5,
    max_delay: float = 8.0,
    on_retry: Optional[Callable[[int, BaseException], None]] = None,
) -> Any:
    """Run ``fn()`` with exponential backoff on transient errors.

    ``attempts`` total tries (default 3). Delays are
    ``min(max_delay, base_delay * 2 ** (attempt - 1))``. Non-transient
    errors propagate immediately.
    """
    last_exc: Optional[BaseException] = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except BaseException as exc:  # noqa: BLE001  -- need to rethrow non-transient
            last_exc = exc
            if not _is_transient(exc) or attempt >= attempts:
                raise
            delay = min(max_delay, base_delay * (2 ** (attempt - 1)))
            if on_retry:
                try:
                    on_retry(attempt, exc)
                except Exception:
                    pass
            logger.debug(
                "Retrying after transient error (attempt %d/%d, sleep %.1fs): %s",
                attempt, attempts, delay, exc,
            )
            time.sleep(delay)
    # Unreachable, but mypy-friendly.
    if last_exc is not None:
        raise last_exc


def retryable(
    *,
    attempts: int = 3,
    base_delay: float = 0.5,
    max_delay: float = 8.0,
):
    """Decorator wrapping a method/function with :func:`with_retries`."""
    def _wrap(fn):
        @functools.wraps(fn)
        def _inner(*args, **kwargs):
            return with_retries(
                lambda: fn(*args, **kwargs),
                attempts=attempts,
                base_delay=base_delay,
                max_delay=max_delay,
            )
        return _inner
    return _wrap
