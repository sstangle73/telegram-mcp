"""One-time sign-in: makes the Telegram session string server.py uses, and writes it to a file (never the screen).

    python login.py                          API_ID and API_HASH from the environment, or asked for
    python login.py --out data/session.txt   the default, or TELEGRAM_SESSION_FILE
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
import sys

from telethon import TelegramClient, utils
from telethon.sessions import StringSession

DEVICE = {"device_model": "telegram-mcp", "system_version": "Claude connector", "app_version": "1.0"}   # server.py's


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default=os.environ.get("TELEGRAM_SESSION_FILE", "data/session.txt"))
    args = ap.parse_args()
    if os.path.exists(args.out):
        sys.exit(f"{args.out} already exists, and may be a live session: move it away (or end that session in "
                 "Telegram's Settings > Devices) first")

    api_id = os.environ.get("API_ID") or input("API ID (my.telegram.org > API development tools): ").strip()
    api_hash = os.environ.get("API_HASH") or getpass.getpass("API hash (hidden): ").strip()
    client = TelegramClient(StringSession(), int(api_id), api_hash, receive_updates=False, **DEVICE)
    await client.start(phone=lambda: input("Phone number, with country code (+1...): ").strip(),
                       code_callback=lambda: input("The login code Telegram sent to your app: ").strip(),
                       password=lambda: getpass.getpass("Two-step verification password (hidden): "))
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
