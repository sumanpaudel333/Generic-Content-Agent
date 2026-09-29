"""
Saves the mail relay's TLS certificate so SMTP_TLS_VERIFY can stay on.

A relay with a self-signed certificate fails verification against the system
trust store. Rather than turning verification off, trust that one certificate
explicitly: this connects to the relay configured in .env, completes the same
STARTTLS (or implicit-SSL) handshake chat_insights.mailer uses, and writes the
certificate it presents to a PEM file for SMTP_TLS_CA_FILE.

    python -m scripts.fetch_smtp_cert                    # writes config/smtp_relay_cert.pem
    python -m scripts.fetch_smtp_cert --out other.pem
    python -m scripts.fetch_smtp_cert --show             # print details, write nothing

No mail is sent -- this is a handshake and a disconnect.

If the relay's certificate is reissued (a new one from create-cert.sh, or an
expiry), re-run this. Verification failing again after a reissue is the system
working: it means the certificate on the wire changed.
"""
import argparse
import os
import socket
import ssl
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from chat_insights import mailer

DEFAULT_OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "config", "smtp_relay_cert.pem")


def fetch_der(host: str, port: int, security: str) -> bytes:
    """Returns the relay's certificate in DER form.

    Verification is deliberately off here: the certificate is being fetched
    precisely because it is not yet trusted. It is inspected before use, and
    from then on it is the thing doing the verifying.
    """
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE

    if security == "ssl":
        with socket.create_connection((host, port), timeout=mailer.SMTP_TIMEOUT_SECONDS) as raw:
            with context.wrap_socket(raw, server_hostname=host) as tls:
                return tls.getpeercert(binary_form=True)

    # STARTTLS: speak just enough SMTP to get the connection upgraded.
    import smtplib
    server = smtplib.SMTP(host, port, timeout=mailer.SMTP_TIMEOUT_SECONDS)
    try:
        server.ehlo()
        if not server.has_extn("starttls"):
            raise RuntimeError(
                f"{host}:{port} does not offer STARTTLS, so it has no certificate to "
                f"fetch. Set SMTP_SECURITY=none in .env for this relay.")
        server.starttls(context=context)
        return server.sock.getpeercert(binary_form=True)
    finally:
        try:
            server.close()
        except Exception:
            pass


def describe(der: bytes) -> dict:
    """Subject/issuer/validity, read back from the parsed certificate."""
    pem = ssl.DER_cert_to_PEM_cert(der)
    # _ssl has no public DER parser, so bounce the PEM through a temp file.
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False, encoding="ascii") as f:
        f.write(pem)
        tmp = f.name
    try:
        info = ssl._ssl._test_decode_cert(tmp)
    finally:
        os.unlink(tmp)

    def flatten(field):
        return ", ".join(f"{k}={v}" for rdn in info.get(field, ()) for k, v in rdn)

    subject, issuer = flatten("subject"), flatten("issuer")
    names = [v for k, v in info.get("subjectAltName", ()) if k in ("DNS", "IP Address")]
    return {"pem": pem, "subject": subject, "issuer": issuer,
            "self_signed": subject == issuer, "alt_names": names,
            "not_after": info.get("notAfter", "")}


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch the SMTP relay's TLS certificate.")
    parser.add_argument("--out", default=DEFAULT_OUT, help=f"where to write the PEM (default: {DEFAULT_OUT})")
    parser.add_argument("--show", action="store_true", help="print the certificate's details without writing it")
    args = parser.parse_args()

    host, port, security = mailer.host(), mailer.port(), mailer.security()
    if not host:
        print("No mail server configured. Set SMTP_HOST in .env first.")
        return 1
    if security == "none":
        print(f"SMTP_SECURITY=none, so the connection to {host}:{port} is not encrypted "
              f"and there is no certificate to fetch. Set SMTP_SECURITY=starttls (or ssl) "
              f"first if the relay supports it.")
        return 1

    print(f"Connecting to {host}:{port} ({security})...")
    try:
        cert = describe(fetch_der(host, port, security))
    except Exception as e:
        print(f"Could not fetch a certificate: {e}")
        return 1

    print(f"  subject:    {cert['subject']}")
    print(f"  issuer:     {cert['issuer']}"
          f"{'  (self-signed)' if cert['self_signed'] else ''}")
    if cert["alt_names"]:
        print(f"  valid for:  {', '.join(cert['alt_names'])}")
    print(f"  expires:    {cert['not_after']}")

    if args.show:
        return 0

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="ascii") as f:
        f.write(cert["pem"])
    print(f"\nWritten to {args.out}")
    print("Set this in .env, and leave verification on:")
    print(f"  SMTP_TLS_CA_FILE={args.out}")
    print( "  SMTP_TLS_VERIFY=true")
    if cert["self_signed"]:
        print("\nThis certificate is self-signed, so it is trusted only for this exact "
              "certificate -- reissue it and this file must be refreshed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
