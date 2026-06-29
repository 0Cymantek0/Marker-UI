"""Local endpoint validation for Hybrid OCR."""

from __future__ import annotations

import ipaddress
from urllib.parse import urlparse


def is_local_endpoint(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return False
    host = parsed.hostname
    if not host:
        return False
    if host in {"localhost"}:
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback


def require_local_endpoint(url: str) -> None:
    if not is_local_endpoint(url):
        raise ValueError("Hybrid OCR specialist endpoint must be localhost/loopback by default.")
