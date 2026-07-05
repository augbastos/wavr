"""Pure rtsp-URL host helpers for F3 camera IP-drift rebind.

Two small, defensive, string-only functions shared by add-time MAC resolution
(wavr.app) and the rebind route:

  * `rtsp_host(url)`  -> the bare host (scheme, user:pass creds, :port and /path
    stripped), or None on an odd shape.
  * `rebind_rtsp_host(url, new_ip)` -> `url` with ONLY the host swapped for
    new_ip; scheme, creds, port and path are preserved byte-for-byte.

Both are hardened: any shape they can't confidently parse yields None (host) or
the ORIGINAL url unchanged (rebind) -- they never raise. Neither EVER logs the
url (it can carry camera credentials -- ADR: creds never leave the box / never
land in a log). Kept tiny + credential-agnostic on purpose: no urllib round-trip
(which URL-decodes then re-encodes creds and can corrupt a password) -- host
surgery is done directly on the string so the rest is untouched.
"""
from __future__ import annotations


def _split_authority(url: str) -> tuple[str, str, str] | None:
    """Return (scheme, authority, tail) for `scheme://authority[tail]` where tail
    is the path/query/fragment (leading '/','?','#' kept). None if there is no
    '://'. authority still includes any `user:pass@` prefix and `:port` suffix."""
    scheme, sep, rest = url.partition("://")
    if not sep:
        return None
    cut = len(rest)
    for ch in "/?#":
        i = rest.find(ch)
        if i != -1 and i < cut:
            cut = i
    return scheme, rest[:cut], rest[cut:]


def rtsp_host(url: str) -> str | None:
    """Extract the host from an rtsp URL (creds, :port and /path stripped).
    Returns None on an odd shape (no '://', empty host). Never raises/logs."""
    try:
        parts = _split_authority(url or "")
        if parts is None:
            return None
        _scheme, authority, _tail = parts
        hostport = authority.rpartition("@")[2]     # drop any user:pass@ prefix
        if not hostport:
            return None
        if hostport.startswith("["):                # [ipv6]:port
            end = hostport.find("]")
            return hostport[1:end] or None if end != -1 else None
        host = hostport.rpartition(":")[0] or hostport   # drop :port if present
        return host or None
    except Exception:
        return None


def rebind_rtsp_host(url: str, new_ip: str) -> str:
    """Return `url` with ONLY its host replaced by `new_ip`; scheme, user:pass
    creds, :port and path/query are preserved byte-for-byte. Defensive: any shape
    it can't confidently rewrite (no '://', no host) is returned UNCHANGED, so a
    caller can detect a no-op by `result == url`. Never raises, never logs url."""
    try:
        parts = _split_authority(url or "")
        if parts is None or not (new_ip or "").strip():
            return url
        scheme, authority, tail = parts
        creds, at, hostport = authority.rpartition("@")
        prefix = f"{creds}{at}" if at else ""
        if not hostport:
            return url
        if hostport.startswith("["):                # [ipv6](:port)
            end = hostport.find("]")
            if end == -1:
                return url
            port = hostport[end + 1:]               # ":port" or ""
        else:
            host, colon, tailport = hostport.rpartition(":")
            port = f":{tailport}" if colon else ""  # no ':' -> whole was the host
        return f"{scheme}://{prefix}{new_ip}{port}{tail}"
    except Exception:
        return url
