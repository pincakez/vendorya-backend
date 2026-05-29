"""Client-IP resolution + login IP allowlist helpers.

Vendorya runs behind a Cloudflare named tunnel, so Django's REMOTE_ADDR is always
127.0.0.1. The real client IP arrives in the ``CF-Connecting-IP`` header, which is
set by Cloudflare and cannot be spoofed by the client (Cloudflare overwrites it).
We trust that header first and fall back to REMOTE_ADDR for local/non-tunnel access.
"""
import ipaddress
import re


def get_client_ip(request):
    """Best-effort real client IP. Prefers Cloudflare's CF-Connecting-IP."""
    cf = request.META.get('HTTP_CF_CONNECTING_IP')
    if cf:
        return cf.strip()
    return request.META.get('REMOTE_ADDR', '') or ''


def parse_allowlist(raw):
    """Split a raw allowlist string (newline/comma separated) into entries."""
    if not raw:
        return []
    return [e.strip() for e in re.split(r'[\n,]+', raw) if e.strip()]


def validate_allowlist(raw):
    """Raise ValueError if any entry is not a valid IP or CIDR network."""
    for entry in parse_allowlist(raw):
        try:
            ipaddress.ip_network(entry, strict=False)
        except ValueError:
            raise ValueError(f"'{entry}' is not a valid IP address or CIDR range.")


def ip_allowed(raw, ip):
    """True if ``ip`` matches any entry in the allowlist. Empty allowlist = unrestricted."""
    entries = parse_allowlist(raw)
    if not entries:
        return True
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    for entry in entries:
        try:
            if addr in ipaddress.ip_network(entry, strict=False):
                return True
        except ValueError:
            continue
    return False
