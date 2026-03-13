"""
Example: pyTelegramBotAPI (telebot) integration
===============================================
For users of the popular telebot library.

Setup:
    pip install pyTelegramBotAPI pyrogram yt-dlp aiohttp

Required env vars:
    BOT_TOKEN, API_ID, API_HASH, SESSION_STRING
"""

import asyncio
import logging
import os

import telebot
from telebot import types

from mtproto_uploader import VideoUploadPipeline

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.environ["BOT_TOKEN"]
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
SESSION_STRING = os.environ.get("SESSION_STRING")

bot = telebot.TeleBot(BOT_TOKEN)

pipeline = VideoUploadPipeline(
    api_id=API_ID,
    api_hash=API_HASH,
    session_string=SESSION_STRING,
    download_dir="/tmp/yt_downloads",
    auto_cleanup=True,
)

# Shared event loop for running async pipeline from sync telebot
loop = asyncio.new_event_loop()


def run_async(coro):
    """Run an async coroutine from sync telebot handlers."""
    return loop.run_until_complete(coro)


@bot.message_handler(commands=["start"])
def start(message: types.Message):
    bot.reply_to(
        message,
        "🎬 YouTube MTProto Uploader Bot\n\n"
        "Send me a YouTube/Vimeo/TikTok link and I'll upload it via MTProto (up to 2GB)!"
    )


@bot.message_handler(commands=["info"])
def info(message: types.Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(message, "Usage: /info <url>")
        return

    url = parts[1]
    status = bot.reply_to(message, "🔍 Fetching info...")

    try:
        video_info = run_async(pipeline.get_video_info(url))
        bot.edit_message_text(
            f"🎬 {video_info.title}\n"
            f"⏱ {video_info.duration_human}\n"
            f"📺 {video_info.resolution}\n"
            f"📦 ~{video_info.filesize_mb:.1f} MB\n"
            f"👤 {video_info.uploader}",
            chat_id=status.chat.id,
            message_id=status.message_id,
        )
    except Exception as e:
        bot.edit_message_text(
            f"❌ Error: {e}",
            chat_id=status.chat.id,
            message_id=status.message_id,
        )


@bot.message_handler(
    func=lambda m: any(d in (m.text or "") for d in ["youtube.com", "youtu.be", "vimeo.com", "tiktok.com"])
)
def handle_url(message: types.Message):
    url = message.text.strip()
    status = bot.reply_to(message, "⏳ Starting download...")

    try:
        result = run_async(pipeline.process(
            url=url,
            chat_id=message.chat.id,
            quality="720p",
            reply_to_message_id=message.message_id,
            # Live progress via bot token:
            bot_token=BOT_TOKEN,
            status_chat_id=message.chat.id,
            status_message_id=status.message_id,
        ))

        bot.edit_message_text(
            f"✅ Done! {result.video_info.filesize_mb:.1f} MB "
            f"in {result.total_duration_seconds:.0f}s",
            chat_id=status.chat.id,
            message_id=status.message_id,
        )

    except Exception as e:
        bot.edit_message_text(
            f"❌ Failed: {e}",
            chat_id=status.chat.id,
            message_id=status.message_id,
        )


def main():
    # Start the async pipeline in the shared event loop
    run_async(pipeline.start())
    print("MTProto pipeline connected. Starting bot polling...")
    try:
        bot.infinity_polling()
    finally:
        run_async(pipeline.stop())


if __name__ == "__main__":
    main()
