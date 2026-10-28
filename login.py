"""One-time sign-in: makes the Telegram session string server.py uses, and writes it to a file (never the screen).

    python login.py                          API_ID and API_HASH from the environment or .env, or asked for
    python login.py --out data/session.txt   the default, or TELEGRAM_SESSION_FILE
    python login.py --country-code 44        for a phone number typed without +44 (the default is 1: US, Canada)
    docker compose run --rm telegram-mcp python login.py      the same, inside the container, into ./data

It asks for your phone number, the code Telegram sends to your app, and your two-step verification password if you
have one. Run it yourself, on your own PC. The session it makes can do anything your Telegram account can; it shows
in Telegram under Settings > Devices as "telegram-mcp", where you can end it. `python deploy.py --apply
--session-file data/session.txt` then copies it to AWS.
"""
import argparse
import asyncio
import getpass
import os
import re
import sys
from pathlib import Path

from telethon import TelegramClient, errors, utils
from telethon.sessions import StringSession

DEVICE = {"device_model": "telegram-mcp", "system_version": "Claude connector", "app_version": "1.0"}   # server.py's


def setting(name: str, env: Path = Path(__file__).resolve().with_name(".env")) -> str | None:
    """From the environment, else from the .env beside this script (KEY=value lines), as deploy.py reads it."""
    if os.environ.get(name):
        return os.environ[name]
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            k, sep, v = line.strip().partition("=")
            if sep and k == name and v.strip():
                return v.strip().strip("\"'")
    return None


def international(raw: str, country_code: str) -> str:
    """A phone number as Telegram wants it, +<country code><number>. Without a + or 00 the country code is assumed,
    and a national number's leading 0 is dropped: 07700 900123 with country code 44 is +447700900123."""
    s = raw.strip()
    digits = re.sub(r"\D", "", s)
    if s.startswith("+"):
        return "+" + digits
    if digits.startswith("00"):
        return "+" + digits[2:]
    if digits.startswith(country_code) and len(digits) == len(country_code) + 10:
        return "+" + digits     # 1 555 123 4567: the country code is already there
    return "+" + country_code + digits.lstrip("0")


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default=os.environ.get("TELEGRAM_SESSION_FILE", "data/session.txt"))
    ap.add_argument("--country-code", default="1", help="assumed when the phone number has no + (default 1)")
    args = ap.parse_args()
    cc = args.country_code.lstrip("+")
    if not re.fullmatch(r"[1-9]\d{0,2}", cc):
        sys.exit("--country-code: 1 to 3 digits, such as 1 or 44")
    if os.path.exists(args.out):
        sys.exit(f"{args.out} already exists, and may be a live session: move it away (or end that session in "
                 "Telegram's Settings > Devices) first")

    api_id = setting("API_ID") or input("API ID (my.telegram.org > API development tools): ").strip()
    api_hash = setting("API_HASH") or getpass.getpass("API hash (hidden): ").strip()
    client = TelegramClient(StringSession(), int(api_id), api_hash, receive_updates=False, **DEVICE)
    while True:
        phone = international(input(f"Phone number (+{cc} unless you type another +country code): "), cc)
        if len(phone) < 8:
            continue
        print(f"Asking Telegram to send a login code for {phone}.")
        try:
            await client.start(phone=phone,
                               code_callback=lambda: input("The login code Telegram sent to your app: ").strip(),
                               password=lambda: getpass.getpass("Two-step verification password (hidden): "))
            break
        except errors.PhoneNumberInvalidError:   # nothing was sent, so asking again is safe
            print(f"Telegram says {phone} isn't a valid number. Type it again, starting with + and the country code "
                  f"if it isn't +{cc}.")
    me = await client.get_me()
    session = client.session.save()
    await client.disconnect()

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        f.write(session + "\n")
    print(f"Signed in as {utils.get_display_name(me)}. Session ({len(session)} characters) written to {args.out}; "
          "treat it like your Telegram password.")


if __name__ == "__main__":
    asyncio.run(main())
