"""Check that production still refuses unauthenticated and CSRF-less requests.

    python scripts/prod_guards.py {--url <app address> | --url-file <file>} --password-file <secret file>
        [--min-tls-days 14] [--ca-file ca.pem] [--require-current]
    python scripts/prod_guards.py --check-secret --password-file <secret file>

`--check-secret` sends nothing: it only reports whether the secret file is
readable and not empty (exit 0 ok, 1 missing), so a release checks it before
dispatch (#3768).

Probes have no effect even if accepted: a GET of diagnostics without a session,
and a logout of the probe's own session without the CSRF token. For an https
address it also reports the TLS certificate's days to expiry. Exit 0 all
guards hold, 1 a guard failed or the certificate expires soon, 2 unreachable.
"""

from __future__ import annotations

import argparse
import socket
import ssl
import sys
import time
from urllib.parse import urlsplit

from prod_client import ProdError, Session, app_url, checkout_current, read_secret


def check_secret(path: str) -> int:
    """Exit 0 when the secret file lies in an allowed folder, is readable and
    not empty, else 1; its content is neither kept nor printed (#3768)."""
    try:
        read_secret(path)
    except ProdError as exc:
        print(f"FAIL {exc}")
        return 1
    print("ok secret readable")
    return 0


def tls_days(url: str, ca_file: str | None) -> float:
    parts = urlsplit(url)
    host, port = parts.hostname or "", parts.port or 443
    context = ssl.create_default_context(cafile=ca_file)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    try:
        with (
            # 15 s only bounds a hung handshake.
            socket.create_connection((host, port), timeout=15) as raw,
            context.wrap_socket(raw, server_hostname=host) as tls,
        ):
            cert = tls.getpeercert()
    except (OSError, ssl.SSLError) as exc:
        raise ProdError(f"TLS handshake failed ({type(exc).__name__})") from None
    expires = ssl.cert_time_to_seconds(str((cert or {}).get("notAfter")))
    return (expires - time.time()) / 86400


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--url")
    target.add_argument("--url-file")
    # Only whether the secret file is readable and not empty; no request (#3768).
    target.add_argument("--check-secret", action="store_true")
    parser.add_argument("--password-file", required=True)
    # Fewer days to certificate expiry fail the check (exit 1).
    parser.add_argument("--min-tls-days", type=float, default=14)
    parser.add_argument("--ca-file")
    parser.add_argument("--require-current", action="store_true")
    args = parser.parse_args(argv)
    if args.check_secret:
        return check_secret(args.password_file)
    if not checkout_current(args.require_current):
        return 2
    results: list[tuple[bool, str]] = []
    try:
        url = app_url(args.url, args.url_file)
        anonymous = Session(url, args.ca_file)
        code = anonymous.status("GET", "/api/diagnostics")
        results.append((code == 401, f"no session -> {code} (expected 401)"))
        session = Session(url, args.ca_file)
        session.login(args.password_file)
        code = session.status("POST", "/api/logout", body={})
        results.append((code == 403, f"no CSRF token -> {code} (expected 403)"))
        code = session.logout()
        results.append((code == 200, f"logout with CSRF token -> {code}"))
        if urlsplit(session.url).scheme == "https":
            days = tls_days(session.url, args.ca_file)
            results.append(
                (
                    days >= args.min_tls_days,
                    f"TLS certificate expires in {days:.0f} days",
                )
            )
    except ProdError as exc:
        for ok, text in results:
            print(f"{'ok' if ok else 'FAIL'} {text}")
        print(f"FAIL {exc}")
        return 2
    for ok, text in results:
        print(f"{'ok' if ok else 'FAIL'} {text}")
    return 0 if all(ok for ok, _ in results) else 1


if __name__ == "__main__":
    sys.exit(main())
