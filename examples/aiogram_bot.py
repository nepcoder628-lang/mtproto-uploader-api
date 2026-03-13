"""
Example: aiogram 3.x integration
=================================

Setup:
    pip install aiogram pyrogram yt-dlp aiohttp

Required env vars:
    BOT_TOKEN, API_ID, API_HASH, SESSION_STRING
"""

import asyncio
import logging
import os

from aiogram import Bot, Dispatcher, types, Router, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message

from mtproto_uploader import VideoUploadPipeline

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.environ["BOT_TOKEN"]
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
SESSION_STRING = os.environ.get("SESSION_STRING")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
router = Router()
dp.include_router(router)

pipeline = VideoUploadPipeline(
    api_id=API_ID,
    api_hash=API_HASH,
    session_string=SESSION_STRING,
    download_dir="/tmp/yt_downloads",
    auto_cleanup=True,
)


@router.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "🎬 <b>YouTube MTProto Uploader</b>\n\n"
        "Send a YouTube/Vimeo/TikTok URL to upload via MTProto (up to 2GB)!\n\n"
        "/info &lt;url&gt; — Video info\n"
        "/quality &lt;url&gt; — Available qualities",
        parse_mode="HTML",
    )


@router.message(Command("info"))
async def cmd_info(message: Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Usage: /info <url>")
        return

    status = await message.answer("🔍 Fetching info...")
    try:
        info = await pipeline.get_video_info(parts[1])
        await status.edit_text(
            f"🎬 <b>{info.title}</b>\n"
            f"⏱ {info.duration_human} | 📺 {info.resolution}\n"
            f"📦 ~{info.filesize_mb:.1f} MB | 👤 {info.uploader}",
            parse_mode="HTML",
        )
    except Exception as e:
        await status.edit_text(f"❌ Error: {e}")


@router.message(Command("quality"))
async def cmd_quality(message: Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Usage: /quality <url>")
        return

    status = await message.answer("🔍 Checking...")
    try:
        qualities = await pipeline.get_available_qualities(parts[1])
        lines = ["📊 <b>Available Qualities:</b>\n"]
        for q in qualities:
            lines.append(f"• {q['height']}p")
        await status.edit_text("\n".join(lines), parse_mode="HTML")
    except Exception as e:
        await status.edit_text(f"❌ Error: {e}")


@router.message(F.text.regexp(r"https?://"))
async def handle_url(message: Message):
    url = message.text.strip()
    if not any(d in url for d in ["youtube.com", "youtu.be", "vimeo.com", "tiktok.com"]):
        return

    status = await message.answer("⏳ Starting download...")

    try:
        result = await pipeline.process(
            url=url,
            chat_id=message.chat.id,
            quality="720p",
            reply_to_message_id=message.message_id,
            bot_token=BOT_TOKEN,
            status_chat_id=message.chat.id,
            status_message_id=status.message_id,
        )
        await status.edit_text(
            f"✅ Done! {result.video_info.filesize_mb:.1f}MB "
            f"in {result.total_duration_seconds:.0f}s",
            parse_mode="HTML",
        )
    except Exception as e:
        await status.edit_text(f"❌ Failed: {e}")


async def main():
    await pipeline.start()
    print("MTProto pipeline connected. Starting bot polling...")
    try:
        await dp.start_polling(bot)
    finally:
        await pipeline.stop()


if __name__ == "__main__":
    asyncio.run(main())
