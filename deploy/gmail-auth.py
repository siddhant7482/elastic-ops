#!/usr/bin/env python3
"""
Get a Gmail refresh token for alerting. Run this ON A MACHINE WITH A BROWSER.

    python gmail-auth.py

That is usually a laptop rather than a hypervisor, because Google's consent
flow needs a browser and redirects to localhost — the out-of-band "copy this
code" option was withdrawn in 2022.

This is deliberately separate from app.py. The portal targets Python 3.11+
for tomllib, and the machine with the browser is often on something older;
there is no reason a one-off OAuth dance should need the config file, the
database, or anything but the standard library.

It prints the exact lines to paste into .env on the host that sends mail.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                os.pardir, "checker"))

import mailer  # noqa: E402  — path set above


def main():
    print("Gmail alerting — one-time setup\n")
    print("Before this works you need, in the Google Cloud console:")
    print("  1. the Gmail API enabled on the project")
    print("  2. an OAuth client of type 'Desktop app'")
    print("  3. the redirect URI printed below added to that client\n")
    mailer.interactive_auth(os.environ.get("GMAIL_CLIENT_ID", ""),
                            os.environ.get("GMAIL_CLIENT_SECRET", ""))


if __name__ == "__main__":
    main()
