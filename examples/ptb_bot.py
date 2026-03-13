"""
Example: python-telegram-bot v20+ integration
=============================================
This shows how to add MTProto uploading to an existing PTB bot.

The key insight:
- python-telegram-bot uses its own Bot API HTTP calls (50MB limit)
- MTProtoUploader uses Pyrogram's MTProto directly (2GB limit)
- Both run in the same asyncio event loop
- You just call pipeline.process() from your PTB handler

Setup:
    pip install python-telegram-bot pyrogram yt-dlp aiohttp fastapi uvicorn

Required env vars:
    BOT_TOKEN       - Your bot token from @BotFather
    API_ID          - From https://my.telegram.org
    API_HASH        - From https://my.telegram.org
    SESSION_STRING  - Run generate_session.py first to get this
"""

import asyncio
import logging
import os

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.constants import ParseMode

from mtproto_uploader import VideoUploadPipeline

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Configuration ──────────────────────────────────────────────────────────
BOT_TOKEN = os.environ["BOT_TOKEN"]
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
SESSION_STRING = os.environ.get("SESSION_STRING")  # or leave None for phone auth

# ── Initialize pipeline (shared across all handlers) ───────────────────────
pipeline = VideoUploadPipeline(
    api_id=API_ID,
    api_hash=API_HASH,
    session_string=SESSION_STRING,
    download_dir="/tmp/yt_downloads",
    auto_cleanup=True,
)


# ── Handlers ───────────────────────────────────────────────────────────────

async def start_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎬 <b>YouTube MTProto Uploader Bot</b>\n\n"
        "Send me a YouTube URL and I'll upload the video directly to Telegram "
        "using MTProto (up to 2GB)!\n\n"
        "Commands:\n"
        "/quality &lt;url&gt; — Show available qualities\n"
        "/info &lt;url&gt; — Show video info\n\n"
        "Just paste a YouTube link to start!",
        parse_mode=ParseMode.HTML,
    )


async def info_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show video info without downloading."""
    args = ctx.args
    if not args:
        await update.message.reply_text("Usage: /info <url>")
        return

    url = args[0]
    msg = await update.message.reply_text("🔍 Fetching info...")

    try:
        info = await pipeline.get_video_info(url)
        await msg.edit_text(
            f"🎬 <b>{info.title}</b>\n\n"
            f"⏱ Duration: {info.duration_human}\n"
            f"📺 Resolution: {info.resolution}\n"
            f"📦 Approx size: {info.filesize_mb:.1f} MB\n"
            f"👁 Views: {info.view_count:,}\n"
            f"👤 Uploader: {info.uploader}\n\n"
            f"<i>{info.description[:300]}...</i>",
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        await msg.edit_text(f"❌ Error: {e}")


async def quality_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """List available qualities."""
    args = ctx.args
    if not args:
        await update.message.reply_text("Usage: /quality <url>")
        return

    url = args[0]
    msg = await update.message.reply_text("🔍 Checking qualities...")

    try:
        qualities = await pipeline.get_available_qualities(url)
        lines = ["📊 <b>Available Qualities:</b>\n"]
        for q in qualities:
            size_str = f"{q['filesize'] / 1e6:.0f}MB" if q.get("filesize") else "?"
            lines.append(
                f"• <code>{q['height']}p</code>  {q.get('fps', '?')}fps  ~{size_str}"
            )
        await msg.edit_text("\n".join(lines), parse_mode=ParseMode.HTML)
    except Exception as e:
        await msg.edit_text(f"❌ Error: {e}")


async def url_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    Main handler: detect YouTube URL in message and upload via MTProto.
    """
    text = update.message.text.strip()

    # Quick URL validation
    if not any(d in text for d in ["youtube.com", "youtu.be", "vimeo.com", "tiktok.com"]):
        await update.message.reply_text(
            "Please send a valid YouTube/Vimeo/TikTok URL."
        )
        return

    # Send initial status message (we'll edit this with progress)
    status_msg = await update.message.reply_text(
        "⏳ Starting download...",
        reply_to_message_id=update.message.message_id,
    )

    try:
        result = await pipeline.process(
            url=text,
            chat_id=update.effective_chat.id,
            quality="720p",          # Change as needed
            reply_to_message_id=update.message.message_id,
            # Live progress editing:
            bot_token=BOT_TOKEN,
            status_chat_id=update.effective_chat.id,
            status_message_id=status_msg.message_id,
        )

        # Edit status message to show completion
        await status_msg.edit_text(
            f"✅ Uploaded successfully!\n"
            f"📦 {result.video_info.filesize_mb:.1f} MB in "
            f"{result.total_duration_seconds:.0f}s "
            f"({result.average_speed_mbps:.1f} MB/s avg)",
            parse_mode=ParseMode.HTML,
        )

    except Exception as e:
        logger.exception("Upload failed")
        await status_msg.edit_text(f"❌ Failed: {e}")


# ── Main ───────────────────────────────────────────────────────────────────

async def main():
    # Start the MTProto pipeline BEFORE starting the bot
    await pipeline.start()
    logger.info("MTProto pipeline connected.")

    # Export and print session string (only needed once)
    if not SESSION_STRING:
        session = await pipeline.export_session()
        print(f"\n✅ SESSION STRING (save this as SESSION_STRING env var):\n{session}\n")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_handler))
    app.add_handler(CommandHandler("info", info_handler))
    app.add_handler(CommandHandler("quality", quality_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, url_handler))

    logger.info("Bot started.")
    await app.run_polling()

    await pipeline.stop()


if __name__ == "__main__":
    asyncio.run(main())
