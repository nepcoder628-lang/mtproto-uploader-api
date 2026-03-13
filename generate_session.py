"""
Generate Session String
=======================
Run this ONCE to authenticate your Telegram user account and get a session string.
The session string lets the uploader run without re-authenticating.

Usage:
    python generate_session.py

Then set the printed SESSION_STRING as an environment variable.
"""

import asyncio
import os

from pyrogram import Client


async def main():
    print("=" * 60)
    print("MTProto Uploader — Session String Generator")
    print("=" * 60)
    print()
    print("You need your API credentials from https://my.telegram.org")
    print()

    api_id = input("Enter API_ID: ").strip()
    api_hash = input("Enter API_HASH: ").strip()

    print()
    print("Login options:")
    print("  1. User account (phone number) — recommended for large file uploads")
    print("  2. Bot token — limited to ~50MB but still works via MTProto")
    choice = input("Choose [1/2]: ").strip()

    async with Client(
        name=":memory:",
        api_id=int(api_id),
        api_hash=api_hash,
        phone_number=input("Phone number (+1234567890): ").strip() if choice == "1" else None,
        bot_token=input("Bot token: ").strip() if choice == "2" else None,
    ) as client:
        session_string = await client.export_session_string()
        me = await client.get_me()
        print()
        print(f"✅ Authenticated as: {me.first_name} (id={me.id})")
        print()
        print("=" * 60)
        print("SESSION STRING (save this securely!):")
        print("=" * 60)
        print(session_string)
        print("=" * 60)
        print()
        print("Add to your .env file:")
        print(f'SESSION_STRING="{session_string}"')
        print()


if __name__ == "__main__":
    asyncio.run(main())
