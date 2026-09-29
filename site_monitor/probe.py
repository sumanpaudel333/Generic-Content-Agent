"""
One measured request to a monitored page.

Built on the standard library rather than a browser or an HTTP client that
fetches more than it is asked for. A check here is exactly one GET for the
page's HTML: no images, no stylesheets, no scripts, no connection pool left
open, and a hard timeout. That is what keeps five-minute monitoring from
being a load on the shop.

Two ways to reach a page:

    direct   this server's own DNS. For www.bcsands.com.au that is the shop
             server itself on the office network, which is how the monitor
             reaches Zen Cart without meeting Cloudflare's bot check.
             public DNS, then a connection presenting the real hostname --
             exactly what a customer's browser does. Through Cloudflare.

Both verify the certificate. A monitor that quietly accepted a bad
certificate would say "up" about a page every browser refuses to open.
"""
import http.client
import ipaddress
import json
import secrets
import socket
import ssl
import string
import time
import urllib.request
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

USER_AGENT = "BCSands-SiteMonitor/1.0 (+https://cms.bcsands.com.au/site-health)"
MAX_BYTES = 3_000_000
MAX_REDIRECTS = 3
DOH_URL = "https://1.1.1.1/dns-query"
CONTROL_URL = "https://1.1.1.1/cdn-cgi/trace"


class ProbeError(Exception):
    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


def new_session_id() -> str:
    """A session id of the shape Zen Cart issues. The monitor keeps one and
    reuses it, because opening a new shop session costs the server several
    seconds of work that an existing one does not."""
    alphabet = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(26))


def is_private(ip: str) -> bool:
    try:
        return ipaddress.ip_address(ip).is_private
    except ValueError:
        return False


SESSION_IN_QUERY = "query"    # ?zenid=... and the cookie, what a first-time visitor gets
SESSION_COOKIE_ONLY = "cookie"  # the cookie alone
SESSION_MODES = (SESSION_IN_QUERY, SESSION_COOKIE_ONLY)


def sends_session_in_query(target: dict) -> bool:
    """Whether the session goes in the address as well as the cookie.

    Cookie-only exists for the route through Cloudflare. The WAF rules there
    challenge any address containing "zenid=", but they do not look at cookies,
    and Zen Cart accepts a session from the cookie alone -- so a cookie-only
    check reaches a real shop page without the monitor going anywhere near
    Cloudflare's bot protection. It also keeps the shop's own session, which
    matters: without one, the shop issues a new session on every single
    request, which took about 5 s of its time each.
    """
    return (target.get("session_mode") or SESSION_IN_QUERY) != SESSION_COOKIE_ONLY


def _with_session(url: str, param: str, session_id: str) -> str:
    if not param or not session_id:
        return url
    parts = urlparse(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != param]
    query.append((param, session_id))
    return urlunparse(parts._replace(query=urlencode(query)))


def _resolve_direct(host: str) -> str:
    try:
        infos = socket.getaddrinfo(host, None, socket.AF_INET, socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise ProbeError("dns", f"Could not look up {host}: {e}")
    return infos[0][4][0]


def _resolve_public(host: str, timeout: float) -> str:
    url = f"{DOH_URL}?{urlencode({'name': host, 'type': 'A'})}"
    request = urllib.request.Request(url, headers={"accept": "application/dns-json",
                                                   "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read(200_000).decode("utf-8", "replace"))
    except Exception as e:
        raise ProbeError("dns", f"Public DNS lookup for {host} failed: {e}")
    for answer in data.get("Answer") or []:
        if answer.get("type") == 1 and answer.get("data"):
            return answer["data"]
    raise ProbeError("dns", f"Public DNS has no address for {host}.")


def _open(scheme: str, host: str, ip: str, port: int, timeout: float):
    """A connection to `ip` that presents `host` -- plus (days left, expiry
    date) of the certificate for HTTPS."""
    if scheme == "http":
        conn = http.client.HTTPConnection(ip, port, timeout=timeout)
        conn.connect()
        return conn, None
    raw = socket.create_connection((ip, port), timeout=timeout)
    context = ssl.create_default_context()
    try:
        tls = context.wrap_socket(raw, server_hostname=host)
    except Exception:
        raw.close()
        raise
    cert = tls.getpeercert() or {}
    cert_info = None
    if cert.get("notAfter"):
        expires = datetime.strptime(cert["notAfter"], "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
        cert_info = ((expires - datetime.now(timezone.utc)).days, expires.date().isoformat())
    conn = http.client.HTTPSConnection(host, port, timeout=timeout, context=context)
    conn.sock = tls
    return conn, cert_info


def fetch(target: dict, *, session_id: str = "", timeout: float = 20) -> dict:
    """GETs one page and measures it. Never raises -- failures come back as
    error_kind / error, so the caller always has something to record."""
    param = target.get("session_param") or ""
    in_query = sends_session_in_query(target)
    url = _with_session(target["url"], param, session_id) if in_query else target["url"]
    first_host = urlparse(url).hostname
    result = {"status": None, "ms": None, "final_url": url, "location": "", "ip": "",
              "cert_days": None, "cert_expires": "", "body": "", "bytes": 0,
              "error_kind": "", "error": "", "challenge": False, "cross_host_redirect": False}
    started = time.perf_counter()
    current = url
    try:
        for hop in range(MAX_REDIRECTS + 1):
            parts = urlparse(current)
            host = parts.hostname or ""
            port = parts.port or (443 if parts.scheme == "https" else 80)
            if target.get("route") == "public":
                ip = _resolve_public(host, timeout)
            else:
                ip = _resolve_direct(host)
            result["ip"] = ip
            conn, cert_info = _open(parts.scheme, host, ip, port, timeout)
            path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
            headers = {"Host": host if parts.port is None else f"{host}:{parts.port}",
                       "User-Agent": USER_AGENT, "Accept": "text/html",
                       "Accept-Encoding": "identity", "Connection": "close"}
            if param and session_id:
                headers["Cookie"] = f"{param}={session_id}"
            try:
                conn.request("GET", path, headers=headers)
                response = conn.getresponse()
                body = response.read(MAX_BYTES)
            finally:
                conn.close()
            if cert_info and hop == 0:
                result["cert_days"], result["cert_expires"] = cert_info
            result["status"] = response.status
            result["final_url"] = current
            if response.status in (301, 302, 303, 307, 308):
                nxt = urljoin(current, response.getheader("Location", "") or "")
                result["location"] = nxt
                if urlparse(nxt).hostname != first_host:
                    result["cross_host_redirect"] = True
                    break
                current = _with_session(nxt, param, session_id) if in_query else nxt
                continue
            text = body.decode("utf-8", "replace")
            result["body"] = text
            result["bytes"] = len(body)
            result["challenge"] = response.status in (403, 503) and (
                bool(response.getheader("cf-mitigated"))
                or "just a moment" in text[:5000].lower())
            break
    except ProbeError as e:
        result["error_kind"], result["error"] = e.kind, str(e)
    except ssl.SSLCertVerificationError as e:
        result["error_kind"] = "tls"
        result["error"] = getattr(e, "verify_message", "") or str(e)
    except ssl.SSLError as e:
        result["error_kind"], result["error"] = "tls", str(e)
    except (TimeoutError, socket.timeout):
        result["error_kind"], result["error"] = "timeout", f"No response within {timeout:g} s."
    except (ConnectionError, OSError) as e:
        result["error_kind"], result["error"] = "connect", f"Could not connect: {e}"
    except http.client.HTTPException as e:
        result["error_kind"], result["error"] = "connect", f"The connection broke: {e}"
    result["ms"] = round((time.perf_counter() - started) * 1000)
    return result


def control_ok(timeout: float = 8) -> bool:
    """Whether this server can reach the internet at all. Asked before calling
    an outside page down, so a broken office connection is not reported as
    the site going down."""
    try:
        request = urllib.request.Request(CONTROL_URL, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status == 200
    except Exception:
        return False


def egress_ip(timeout: float = 8) -> str:
    """The public address this server's requests come from, as Cloudflare sees
    it -- the address a Cloudflare allow rule has to name. "" if unknown."""
    try:
        request = urllib.request.Request(CONTROL_URL, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            for line in response.read(5000).decode("utf-8", "replace").splitlines():
                if line.startswith("ip="):
                    return line[3:].strip()
    except Exception:
        pass
    return ""


def classify(target: dict, result: dict, settings: dict) -> tuple[str, str]:
    """(outcome, detail) for one fetch. The detail is a sentence for an email."""
    kind = result.get("error_kind") or ""
    if kind in ("dns", "connect", "timeout"):
        return "down", result.get("error") or "No response."
    if kind == "tls":
        return "cert_invalid", f"The security certificate was rejected: {result.get('error')}"
    status = result.get("status")
    if result.get("challenge"):
        return "blocked", "Cloudflare showed its bot check instead of the page."
    if status is None:
        return "down", result.get("error") or "No response."
    if 300 <= status < 400:
        return "wrong_content", (f"Redirected to {result.get('location') or 'another address'} "
                                 "instead of showing the page.")
    if status >= 500:
        return "server_error", f"The server returned an error (HTTP {status})."
    if status >= 400:
        return "page_error", f"The page returned HTTP {status}."
    body = (result.get("body") or "").lower()
    missing = [m for m in target.get("must_contain", []) if m.lower() not in body]
    if missing:
        return "wrong_content", ("The page loaded but is missing "
                                 + ", ".join(f'"{m}"' for m in missing[:3]) + ".")
    ms = result.get("ms")
    if ms is not None and ms > settings["slow_ms"]:
        return "slow", f"Took {ms / 1000:.1f} s to load."
    return "ok", ""
