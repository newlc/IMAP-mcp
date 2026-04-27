"""Shared fixtures for integration tests.

These tests run against a real IMAP/SMTP server inside a Docker container
managed by testcontainers. They are skipped automatically if Docker isn't
running.

Run only the integration tests::

    pytest tests/integration/ -v

Skip them on a host without Docker::

    pytest tests/ -m 'not integration'

Each test gets a fresh Greenmail instance so state doesn't bleed.
"""

from __future__ import annotations

import socket
import time
from contextlib import contextmanager
from typing import Iterator

import pytest


# ---------------------------------------------------------------------------
# Docker availability gate
# ---------------------------------------------------------------------------


def docker_available() -> bool:
    """Return True if the Docker daemon is reachable.

    testcontainers will happily try to reach Docker even when the daemon
    isn't running, leading to noisy errors. Probing once up-front lets us
    skip the whole module cleanly. Test modules import this and apply it
    via their own ``pytestmark`` -- declaring ``pytestmark`` in conftest
    has no effect.
    """
    try:
        import docker  # type: ignore
    except ImportError:
        return False
    try:
        client = docker.from_env()
        client.ping()
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Greenmail container
# ---------------------------------------------------------------------------


# Greenmail bundles IMAP, SMTP, POP3 and an embedded admin API in one
# tiny container. Image is ~100 MB, starts in <2 s on a warm Docker host.
GREENMAIL_IMAGE = "greenmail/standalone:2.0.1"


def _wait_for_port(host: str, port: int, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1.0):
                return
        except OSError as exc:
            last_exc = exc
            time.sleep(0.5)
    raise RuntimeError(
        f"Port {host}:{port} not reachable within {timeout}s "
        f"(last error: {last_exc})"
    )


@contextmanager
def greenmail_running() -> Iterator[dict]:
    """Yield a dict describing the running Greenmail instance.

    Exposed keys: ``host``, ``imap_port``, ``smtp_port``, ``api_port``,
    ``api_url``.
    """
    from testcontainers.core.container import DockerContainer
    from testcontainers.core.waiting_utils import wait_for_logs

    container = (
        DockerContainer(GREENMAIL_IMAGE)
        # Greenmail listens on these ports inside the container; we let
        # Docker map them to ephemeral host ports.
        .with_exposed_ports(3143, 3025, 8080)
        .with_env("GREENMAIL_OPTS",
                  "-Dgreenmail.setup.test.all "
                  "-Dgreenmail.hostname=0.0.0.0 "
                  "-Dgreenmail.users=alice:alicepass@example.com,"
                  "bob:bobpass@example.com "
                  "-Dgreenmail.auth.disabled=false "
                  "-Dgreenmail.verbose")
    )

    container.start()
    try:
        wait_for_logs(container, "Started GreenMail standalone", timeout=60)
        host = container.get_container_host_ip()
        imap_port = int(container.get_exposed_port(3143))
        smtp_port = int(container.get_exposed_port(3025))
        api_port = int(container.get_exposed_port(8080))
        _wait_for_port(host, imap_port)
        _wait_for_port(host, smtp_port)
        yield {
            "host": host,
            "imap_port": imap_port,
            "smtp_port": smtp_port,
            "api_port": api_port,
            "api_url": f"http://{host}:{api_port}",
        }
    finally:
        container.stop()


@pytest.fixture(scope="module")
def greenmail() -> Iterator[dict]:
    """Module-scoped Greenmail instance.

    Per-module rather than per-test because container startup is the
    biggest cost. Tests should use the API to clean inboxes between
    runs (``GET /api/service/reset``).
    """
    with greenmail_running() as info:
        yield info


@pytest.fixture
def greenmail_clean(greenmail):
    """Reset Greenmail state before each test in a module that needs it."""
    import urllib.request
    try:
        urllib.request.urlopen(
            f"{greenmail['api_url']}/api/service/reset", timeout=5,
        ).read()
    except Exception:
        pass
    yield greenmail


# ---------------------------------------------------------------------------
# Test account config + helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def alice_config(greenmail_clean) -> dict:
    """A ready-to-use single-account config dict pointing at Greenmail."""
    return {
        "imap": {
            "host": greenmail_clean["host"],
            "port": greenmail_clean["imap_port"],
            "secure": False,  # Greenmail's plain IMAP port
        },
        "smtp": {
            "host": greenmail_clean["host"],
            "port": greenmail_clean["smtp_port"],
            "secure": False,
            "starttls": False,
        },
        "credentials": {
            "username": "alice@example.com",
            "password": "alicepass",
        },
        "user": {
            "name": "Alice",
            "email": "alice@example.com",
        },
        "folders": {
            "inbox": "INBOX",
            "drafts": "Drafts",
            "sent": "Sent",
            "trash": "Trash",
        },
        "cache": {
            "enabled": False,
            "encrypt": False,
            "db_path": "/tmp/imap-mcp-integration.db",
        },
        "auto_archive": {"enabled": False},
    }


def deliver_email(
    greenmail_info: dict,
    *,
    sender: str,
    recipient: str,
    subject: str,
    body: str = "Test body",
) -> None:
    """Inject an email via SMTP so the recipient can fetch it via IMAP."""
    import smtplib
    from email.message import EmailMessage
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = recipient
    msg["Subject"] = subject
    msg.set_content(body)
    with smtplib.SMTP(greenmail_info["host"], greenmail_info["smtp_port"]) as s:
        # Greenmail in the configuration above accepts auth from any of
        # the seeded users; SMTP send doesn't strictly need it for local
        # delivery but we add it to exercise the auth path too.
        try:
            s.login("alice", "alicepass")
        except smtplib.SMTPNotSupportedError:
            pass
        s.send_message(msg)
