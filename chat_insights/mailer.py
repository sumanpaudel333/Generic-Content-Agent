"""
Sends the weekly report by email. Stdlib only (smtplib + email.message).

Provider-agnostic: the mail server, port, connection security, credentials and
From address all come from .env (with config.yaml as a fallback for the
non-secret ones), so any SMTP relay works -- Microsoft 365, Google Workspace,
a hosting provider's mail server, SendGrid/Mailgun, or an internal relay.

  .env keys        SMTP_HOST, SMTP_PORT, SMTP_SECURITY, SMTP_USERNAME,
                   SMTP_PASSWORD, SMTP_FROM, SMTP_FROM_NAME, SMTP_RECIPIENTS,
                   SMTP_TLS_CA_FILE, SMTP_TLS_VERIFY
  config.yaml      chat_insights.smtp_host / smtp_port / smtp_security /
                   smtp_from / smtp_from_name / report_recipients /
                   smtp_tls_ca_file / smtp_tls_verify  (used only when the
                   matching .env key is unset)

SMTP_RECIPIENTS is a comma-separated list ("a@x.com, b@x.com"), so who the
report goes to can be changed without touching config.yaml.

SMTP_SECURITY is one of:
  starttls  plain connection upgraded to TLS (the usual choice on port 587)
  ssl       implicit TLS from the first byte (port 465)
  none      no encryption -- only sensible for an internal relay
Left blank it is inferred from the port: 465 -> ssl, anything else -> starttls.

An internal relay often presents a self-signed certificate, which fails
verification against the system trust store ("CERTIFICATE_VERIFY_FAILED:
self-signed certificate"). Two ways out, in order of preference:
  SMTP_TLS_CA_FILE  path to the relay's certificate or your internal CA in PEM
                    form -- the connection stays verified, against your CA
  SMTP_TLS_VERIFY=false
                    encrypt but do not verify. Only for a relay you control on
                    a network you control: it stops a passive eavesdropper but
                    not someone able to impersonate the relay.

Authentication is skipped when SMTP_USERNAME/SMTP_PASSWORD are blank, and also
when the server does not advertise the AUTH extension -- an open internal relay
has nothing to authenticate against, and offering it credentials it never asked
for only produces "SMTP AUTH extension not supported by server".

A send failure never fails the weekly run: the report is already stored and
viewable in the dashboard, exactly as an Odoo publish failure leaves an approved
draft intact.
"""
import logging
import os
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, parseaddr

from config import settings

logger = logging.getLogger("chat_insights.mailer")

SMTP_TIMEOUT_SECONDS = 30
SECURITY_CHOICES = ("starttls", "ssl", "none")
_FALSEY = ("0", "false", "no", "off")


def _env(name: str, fallback: str = "") -> str:
    """.env wins; config.yaml fills the gap. Blank env values count as unset."""
    return (os.environ.get(name) or "").strip() or fallback


def host() -> str:
    return _env("SMTP_HOST", settings.CHAT_SMTP_HOST)


def port() -> int:
    raw = _env("SMTP_PORT", str(settings.CHAT_SMTP_PORT))
    try:
        return int(raw)
    except ValueError:
        logger.warning("SMTP_PORT=%r is not a number -- falling back to 587", raw)
        return 587


def security() -> str:
    """Connection security, inferred from the port when not stated."""
    value = _env("SMTP_SECURITY", settings.CHAT_SMTP_SECURITY).lower()
    if value in SECURITY_CHOICES:
        return value
    if value:
        logger.warning("SMTP_SECURITY=%r is not one of %s -- inferring from the port",
                       value, ", ".join(SECURITY_CHOICES))
    return "ssl" if port() == 465 else "starttls"


def tls_ca_file() -> str:
    return _env("SMTP_TLS_CA_FILE", settings.CHAT_SMTP_TLS_CA_FILE)


def tls_verify() -> bool:
    """Certificate verification. On by default -- turning it off is a decision
    someone has to make explicitly."""
    raw = _env("SMTP_TLS_VERIFY", "true" if settings.CHAT_SMTP_TLS_VERIFY else "false")
    return raw.lower() not in _FALSEY


def username() -> str:
    return _env("SMTP_USERNAME")


def password() -> str:
    return _env("SMTP_PASSWORD")


def uses_auth() -> bool:
    return bool(username() and password())


def sender() -> str:
    """The bare From address. SMTP_FROM may be written either as a plain address
    or as "Name <address>"; only the address comes back here. Falls back to the
    auth username, which is what most relays expect when the From address is not
    stated separately."""
    configured = _env("SMTP_FROM", settings.CHAT_SMTP_FROM) or username()
    return parseaddr(configured)[1] or configured


def sender_name() -> str:
    """The display name shown in the recipient's inbox, so the report reads as
    coming from the automation rather than from a bare mailbox address."""
    explicit = _env("SMTP_FROM_NAME", settings.CHAT_SMTP_FROM_NAME)
    if explicit:
        return explicit
    # A display name written into SMTP_FROM itself still counts.
    return parseaddr(_env("SMTP_FROM", settings.CHAT_SMTP_FROM))[0]


def from_header() -> str:
    """RFC 5322 From value -- "Chatbase-Insight <chat@bcsands.com.au>". Quoting
    of a name containing commas or other specials is formataddr's job."""
    name = sender_name()
    return formataddr((name, sender())) if name else sender()


def recipients() -> list[str]:
    """Who the report goes to. SMTP_RECIPIENTS (comma-separated) wins over
    chat_insights.report_recipients."""
    raw = _env("SMTP_RECIPIENTS")
    if raw:
        return [address.strip() for address in raw.split(",") if address.strip()]
    return list(settings.CHAT_REPORT_RECIPIENTS)


def is_configured() -> bool:
    return not _missing()


def _missing() -> list[str]:
    missing = []
    if not host():
        missing.append("SMTP_HOST (.env) or chat_insights.smtp_host")
    if not recipients():
        missing.append("SMTP_RECIPIENTS (.env) or chat_insights.report_recipients")
    if not sender():
        missing.append("SMTP_FROM (.env) or chat_insights.smtp_from")
    # Half a credential pair is a misconfiguration, not an unauthenticated relay.
    if username() and not password():
        missing.append("SMTP_PASSWORD (.env)")
    if password() and not username():
        missing.append("SMTP_USERNAME (.env)")
    return missing


def config_status() -> str:
    missing = _missing()
    if not missing:
        auth = "authenticated" if uses_auth() else "no auth"
        tls = ""
        if security() != "none":
            if not tls_verify():
                tls = ", cert NOT verified"
            elif tls_ca_file():
                tls = f", cert verified against {os.path.basename(tls_ca_file())}"
        return (f"Email: configured ({from_header()} -> "
                f"{', '.join(recipients())} via "
                f"{host()}:{port()} {security()}{tls}, {auth})")
    return "Email: not configured -- missing " + ", ".join(missing)


def _ssl_context() -> ssl.SSLContext:
    """TLS settings for both starttls and ssl. Verified against the system trust
    store by default, against SMTP_TLS_CA_FILE when one is given, or not at all
    when SMTP_TLS_VERIFY is off."""
    ca_file = tls_ca_file()
    if ca_file and not os.path.exists(ca_file):
        logger.warning("SMTP_TLS_CA_FILE=%s does not exist -- falling back to the "
                       "system trust store.", ca_file)
        ca_file = ""
    context = ssl.create_default_context(cafile=ca_file or None)
    if not tls_verify():
        logger.warning("SMTP_TLS_VERIFY is off -- the connection to %s is encrypted but "
                       "the server's certificate is not checked.", host())
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return context


def _connect():
    """Opens the SMTP connection described by the current settings.

    Always ends on an EHLO, so has_extn() in send() reflects what the server
    actually offers on the *encrypted* channel (STARTTLS resets the feature
    list, and a fresh SMTP_SSL object has not read one at all)."""
    if security() == "ssl":
        server = smtplib.SMTP_SSL(host(), port(), timeout=SMTP_TIMEOUT_SECONDS,
                                  context=_ssl_context())
    else:
        server = smtplib.SMTP(host(), port(), timeout=SMTP_TIMEOUT_SECONDS)
        if security() == "starttls":
            server.ehlo()
            server.starttls(context=_ssl_context())
    server.ehlo()
    return server


def send(subject: str, html_body: str, *, to_addresses: list[str] | None = None,
         smtp_factory=None) -> dict:
    """Sends the report. Returns {"success": bool, "detail": str}.

    `smtp_factory` is injectable so tests exercise the whole path without
    touching a real mail server."""
    to_addresses = to_addresses or recipients()
    if not is_configured():
        return {"success": False, "detail": config_status()}

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = from_header()
    message["To"] = ", ".join(to_addresses)
    message.set_content(
        "This report is formatted in HTML. If you are reading this, your mail client "
        "could not display it -- the same report is available in the Automation Control "
        "dashboard under Chat Insights."
    )
    message.add_alternative(html_body, subtype="html")

    try:
        if smtp_factory is not None:
            server = smtp_factory()
            with server:
                server.send_message(message)
        else:
            with _connect() as server:
                if uses_auth() and server.has_extn("auth"):
                    server.login(username(), password())
                elif uses_auth():
                    # An open relay (typically port 25 on the LAN) accepts mail
                    # without credentials. Send it rather than failing on a
                    # login the server has no way to accept.
                    logger.warning("%s:%s does not offer SMTP AUTH -- sending without "
                                   "authenticating.", host(), port())
                server.send_message(message)
        return {"success": True, "detail": f"Sent to {', '.join(to_addresses)}"}
    except smtplib.SMTPAuthenticationError as e:
        logger.error("SMTP auth failed: %s", e)
        return {"success": False, "detail": (
            f"SMTP authentication failed for {username()} on {host()}:{port()} ({e}). "
            f"Check SMTP_USERNAME/SMTP_PASSWORD in .env. On Microsoft 365, SMTP AUTH is "
            f"disabled by default and must be enabled for the mailbox, with an app "
            f"password used here.")}
    except ssl.SSLCertVerificationError as e:
        logger.error("SMTP TLS verification failed: %s", e)
        return {"success": False, "detail": (
            f"{host()}:{port()} presented a certificate that could not be verified ({e}). "
            f"Point SMTP_TLS_CA_FILE at the relay's certificate in PEM form, or set "
            f"SMTP_TLS_VERIFY=false in .env to encrypt without verifying.")}
    except Exception as e:
        logger.error("SMTP send failed: %s", e)
        return {"success": False, "detail": f"Send failed via {host()}:{port()} ({security()}): {e}"}
