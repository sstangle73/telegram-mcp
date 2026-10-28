"""Set up sign-in: the password and authenticator code you sign in to this server with from Claude. Run it yourself.

    python setup_signin.py                    writes data/signin.env
    docker compose run --rm telegram-mcp python setup_signin.py      the same, inside the container, into ./data

It asks for a new password twice, makes an authenticator (TOTP) secret and, once you say so, shows it for your app
(Bitwarden, 1Password, Aegis, Google Authenticator and the like), then checks a code from the app. It writes
LOGIN_PASSWORD_HASH, TOTP_SECRET and a new SIGNING_KEY to data/signin.env, readable only by you. Then:
  - on Lambda: python deploy.py --apply --signin-file data/signin.env, and delete the file;
  - elsewhere: give the three to your host as secrets, along with PUBLIC_URL (with docker compose, add them to .env).
Running it again makes a new password, secret and key, which signs every client out.
"""
import argparse
import getpass
import os
import secrets
import sys
from urllib.parse import quote

import oauth


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default="data/signin.env")
    ap.add_argument("--label", default="telegram-mcp", help="the name your authenticator app shows for it")
    args = ap.parse_args()
    if os.path.exists(args.out):
        sys.exit(f"{args.out} already exists: move it away first (a new one signs every client out)")

    password = getpass.getpass("New sign-in password, 12 or more characters (hidden): ")
    if len(password) < 12:
        sys.exit("too short: use 12 characters or more (one from a password manager is best)")
    if getpass.getpass("The same again (hidden): ") != password:
        sys.exit("the two didn't match: nothing written")
    secret = oauth.new_totp_secret()
    if input("Show the authenticator setup key on this screen now? [y/N] ").strip().lower() not in ("y", "yes"):
        sys.exit("nothing written: run it again when no one else can see your screen")
    label = quote(args.label)
    print("\nAdd this to your authenticator app as a setup key (time-based, 6 digits, every 30 seconds):\n")
    print("    " + " ".join(secret[i:i + 4] for i in range(0, len(secret), 4)))
    print("\nor give an app that takes links this one:\n")
    print(f"    otpauth://totp/{label}?secret={secret}&issuer={label}&algorithm=SHA1&digits=6&period=30\n")
    for _ in range(3):
        if oauth.check_totp(secret, input("The 6-digit code your app shows now: ")) is not None:
            break
        print("That code doesn't match: check the key you entered, and that the device's clock is right.")
    else:
        sys.exit("nothing written: the app's codes didn't match")
    if sys.stdout.isatty():
        print("\033[3J\033[2J\033[H", end="")   # the setup key off the screen and out of the scrollback
    print("Your authenticator app has it. Clear this terminal's scrollback too, if it keeps one.")

    lines = ["# setup_signin.py's sign-in secrets: never commit or share this, and delete it once your host has them.",
             f"LOGIN_PASSWORD_HASH={oauth.hash_password(password)}", f"TOTP_SECRET={secret}",
             f"SIGNING_KEY={secrets.token_urlsafe(32)}"]
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    print(f"LOGIN_PASSWORD_HASH, TOTP_SECRET and SIGNING_KEY written to {args.out}. Next: on Lambda, python deploy.py "
          f"--apply --signin-file {args.out}; elsewhere, give your host those three and PUBLIC_URL as secrets.")


if __name__ == "__main__":
    main()
