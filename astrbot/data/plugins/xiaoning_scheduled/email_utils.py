"""Secure SMTP delivery for Xiaoning's generated report PDFs."""
from __future__ import annotations

import base64
import os
import re
import socket
import smtplib
import ssl
import time
from email.message import EmailMessage
from pathlib import Path
from typing import Mapping, Any
from urllib.parse import urlparse

from astrbot.api import logger


class _ProxySMTP(smtplib.SMTP):
    """SMTP connection routed through a local HTTP CONNECT proxy."""

    def __init__(self, *args, proxy_url: str, **kwargs):
        self._proxy_url = proxy_url
        super().__init__(*args, **kwargs)

    def _get_socket(self, host, port, timeout):
        proxy = urlparse(self._proxy_url)
        if proxy.scheme.lower() != "http" or not proxy.hostname or not proxy.port:
            raise ValueError("Unsupported XIAONING_OUTBOUND_PROXY URL")
        raw_socket = socket.create_connection((proxy.hostname, proxy.port), timeout)
        destination = f"{host}:{port}"
        headers = [
            f"CONNECT {destination} HTTP/1.1",
            f"Host: {destination}",
            "Proxy-Connection: Keep-Alive",
        ]
        if proxy.username:
            credentials = f"{proxy.username}:{proxy.password or ''}".encode()
            token = base64.b64encode(credentials).decode("ascii")
            headers.append(f"Proxy-Authorization: Basic {token}")
        raw_socket.sendall(("\r\n".join(headers) + "\r\n\r\n").encode("ascii"))
        response = bytearray()
        while b"\r\n\r\n" not in response and len(response) < 16384:
            # SMTP may send its 220 greeting in the same packet. Consume only
            # the HTTP header so smtplib can read the greeting itself.
            chunk = raw_socket.recv(1)
            if not chunk:
                break
            response.extend(chunk)
        status_line = bytes(response).split(b"\r\n", 1)[0]
        if not re.match(br"^HTTP/1\.[01] 200(?:\s|$)", status_line):
            raw_socket.close()
            raise OSError("SMTP proxy CONNECT failed")
        return raw_socket


def send_report_email(
    config: Mapping[str, Any], subject: str, text: str, pdf: Path
) -> bool:
    """Send a report PDF; credentials stay in process environment only."""
    if not config.get("report_email_enabled", False):
        return False
    recipient = str(
        os.environ.get("XIAONING_REPORT_EMAIL_TO")
        or config.get("report_email_to", "")
    ).strip()
    username = str(
        os.environ.get("XIAONING_REPORT_SMTP_USERNAME")
        or config.get("report_smtp_username", "")
    ).strip()
    password = os.environ.get("XIAONING_REPORT_SMTP_PASSWORD", "").strip()
    host = str(
        os.environ.get("XIAONING_REPORT_SMTP_HOST")
        or config.get("report_smtp_host", "smtp.gmail.com")
    ).strip()
    port = int(
        os.environ.get("XIAONING_REPORT_SMTP_PORT")
        or config.get("report_smtp_port", 587)
    )
    if not all((recipient, username, password, host)):
        logger.warning("[小柠定时] 报告邮件未发送: SMTP 私有配置不完整")
        return False

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = username
    message["To"] = recipient
    message.set_content(text)
    message.add_attachment(
        pdf.read_bytes(),
        maintype="application",
        subtype="pdf",
        filename=pdf.name,
    )

    timeout = 30
    context = ssl.create_default_context()
    proxy_url = os.environ.get("XIAONING_OUTBOUND_PROXY", "").strip()
    for attempt in range(3):
        try:
            if proxy_url:
                smtp_client = _ProxySMTP(
                    host, port, timeout=timeout, proxy_url=proxy_url
                )
            else:
                smtp_client = smtplib.SMTP(host, port, timeout=timeout)
            with smtp_client as smtp:
                smtp.ehlo()
                smtp.starttls(context=context)
                smtp.ehlo()
                smtp.login(username, password)
                smtp.send_message(message)
            logger.info("[小柠定时] 报告 PDF 邮件已发送")
            return True
        except Exception as exc:
            logger.warning(
                "[小柠定时] 报告邮件第 %d 次发送失败: %s",
                attempt + 1,
                type(exc).__name__,
            )
            if attempt < 2:
                time.sleep(1 * (2 ** attempt))
    return False
