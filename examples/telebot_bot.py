"""
Telebot bot that uses the MTProto Uploader API
===============================================
This bot calls your hosted API (on Render) to upload videos up to 2GB.
The bot itself is lightweight — no pyrogram, no yt-dlp needed here.

Setup:
    pip install pyTelegramBotAPI requests

Required env vars (.env or export):
    BOT_TOKEN       - Your bot token from @BotFather
    API_ID          - From https://my.telegram.org
    API_HASH        - From https://my.telegram.org
    SESSION_STRING  - From POST /auth/verify on your hosted API
    MTPROTO_API_URL - Your hosted API URL e.g. https://mtproto-uploader.onrender.com

How to get SESSION_STRING (one time only):
    1. Call POST {MTPROTO_API_URL}/auth/send-code with your api_id, api_hash, phone
    2. Call POST {MTPROTO_API_URL}/auth/verify with the OTP you receive
    3. Copy the session_string from the response into your env vars
"""

import os
import logging
import requests
import telebot
from telebot import types

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Config ─────────────────────────────────────────────────────────────────
BOT_TOKEN      = os.environ["BOT_TOKEN"]
API_ID         = int(os.environ["API_ID"])
API_HASH       = os.environ["API_HASH"]
SESSION_STRING = os.environ["SESSION_STRING"]
API_URL        = os.environ.get("MTPROTO_API_URL", "https://mtproto-uploader.onrender.com").rstrip("/")

# Credentials payload — attached to every API call
CREDS = {
    "api_id": API_ID,
    "api_hash": API_HASH,
    "session_string": SESSION_STRING,
}

SUPPORTED_DOMAINS = [
    "youtube.com", "youtu.be", "vimeo.com",
    "tiktok.com", "twitter.com", "x.com",
    "instagram.com", "facebook.com", "dailymotion.com",
]

QUALITY_OPTIONS = ["best", "1080p", "720p", "480p", "360p", "worst", "audio"]

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

# Per-chat state: stores selected quality while user picks
user_quality: dict[int, str] = {}


# ── Helpers ────────────────────────────────────────────────────────────────

def is_supported_url(text: str) -> bool:
    return any(domain in text for domain in SUPPORTED_DOMAINS)


def quality_keyboard() -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=3)
    kb.add(*[
        types.InlineKeyboardButton(q, callback_data=f"quality:{q}")
        for q in QUALITY_OPTIONS
    ])
    return kb


# ── Handlers ───────────────────────────────────────────────────────────────

@bot.message_handler(commands=["start", "help"])
def cmd_start(msg: types.Message):
    bot.reply_to(msg,
        "<b>MTProto Video Uploader Bot</b>\n\n"
        "Send me any video URL and I'll upload it to Telegram (up to 2GB).\n\n"
        "Supported: YouTube, TikTok, Instagram, Twitter/X, Vimeo, Facebook...\n\n"
        "<b>Commands:</b>\n"
        "/info &lt;url&gt; — Get video info without downloading\n"
        "/qualities &lt;url&gt; — List available qualities\n"
        "/upload &lt;url&gt; — Upload at default quality (720p)\n"
    )


@bot.message_handler(commands=["info"])
def cmd_info(msg: types.Message):
    parts = msg.text.split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(msg, "Usage: /info &lt;url&gt;")
        return

    url = parts[1].strip()
    status = bot.reply_to(msg, "Fetching info...")

    try:
        resp = requests.post(f"{API_URL}/info", json={**CREDS, "url": url}, timeout=30)
        resp.raise_for_status()
        d = resp.json()
        bot.edit_message_text(
            f"<b>{d['title']}</b>\n\n"
            f"Duration: {d['duration_human']}\n"
            f"Resolution: {d['width']}x{d['height']}\n"
            f"Size: ~{d['filesize_mb']} MB\n"
            f"Uploader: {d['uploader']}\n"
            f"Views: {d['view_count']:,}",
            chat_id=status.chat.id,
            message_id=status.message_id,
        )
    except requests.HTTPError as e:
        bot.edit_message_text(
            f"Error: {e.response.json().get('detail', str(e))}",
            chat_id=status.chat.id,
            message_id=status.message_id,
        )
    except Exception as e:
        bot.edit_message_text(f"Error: {e}", chat_id=status.chat.id, message_id=status.message_id)


@bot.message_handler(commands=["qualities"])
def cmd_qualities(msg: types.Message):
    parts = msg.text.split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(msg, "Usage: /qualities &lt;url&gt;")
        return

    url = parts[1].strip()
    status = bot.reply_to(msg, "Fetching available qualities...")

    try:
        resp = requests.post(f"{API_URL}/qualities", json={**CREDS, "url": url}, timeout=30)
        resp.raise_for_status()
        qualities = resp.json()["qualities"]
        lines = "\n".join(
            f"• {q['quality']}  {q['width']}x{q['height']}  "
            f"{q['filesize_mb']} MB" if q.get("filesize_mb") else f"• {q['quality']}"
            for q in qualities
        )
        bot.edit_message_text(
            f"<b>Available qualities:</b>\n{lines}",
            chat_id=status.chat.id,
            message_id=status.message_id,
        )
    except requests.HTTPError as e:
        bot.edit_message_text(
            f"Error: {e.response.json().get('detail', str(e))}",
            chat_id=status.chat.id,
            message_id=status.message_id,
        )
    except Exception as e:
        bot.edit_message_text(f"Error: {e}", chat_id=status.chat.id, message_id=status.message_id)


@bot.message_handler(commands=["upload"])
def cmd_upload(msg: types.Message):
    parts = msg.text.split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(msg, "Usage: /upload &lt;url&gt;")
        return
    # Treat as a plain URL message with default quality
    msg.text = parts[1].strip()
    handle_url(msg)


@bot.message_handler(func=lambda m: bool(m.text) and is_supported_url(m.text))
def handle_url(msg: types.Message):
    """User sends a URL — ask for quality then upload."""
    url = msg.text.strip()
    # Store URL in user state
    user_quality[msg.chat.id] = url
    bot.reply_to(
        msg,
        f"Choose quality for:\n<code>{url[:80]}</code>",
        reply_markup=quality_keyboard(),
    )


@bot.callback_query_handler(func=lambda c: c.data.startswith("quality:"))
def on_quality_pick(call: types.CallbackQuery):
    quality = call.data.split(":")[1]
    url = user_quality.pop(call.message.chat.id, None)

    if not url:
        bot.answer_callback_query(call.id, "Session expired. Send the URL again.")
        return

    bot.answer_callback_query(call.id)
    bot.edit_message_text(
        f"Starting download at <b>{quality}</b>...",
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
    )

    _do_upload(
        chat_id=call.message.chat.id,
        url=url,
        quality=quality,
        status_message_id=call.message.message_id,
        reply_to_message_id=call.message.reply_to_message.message_id
            if call.message.reply_to_message else None,
    )


def _do_upload(chat_id: int, url: str, quality: str,
               status_message_id: int, reply_to_message_id: int | None):
    """Call the MTProto API and stream progress back via status message edits."""
    try:
        payload = {
            **CREDS,
            "url": url,
            "chat_id": str(chat_id),
            "quality": quality,
            "bot_token": BOT_TOKEN,
            "status_chat_id": str(chat_id),
            "status_message_id": status_message_id,
        }
        if reply_to_message_id:
            payload["reply_to_message_id"] = reply_to_message_id

        resp = requests.post(
            f"{API_URL}/upload",
            json=payload,
            timeout=600,  # 10 min — allow large file uploads
        )
        resp.raise_for_status()
        d = resp.json()

        bot.edit_message_text(
            f"Done!\n\n"
            f"<b>{d['title']}</b>\n"
            f"Size: {d['filesize_mb']} MB\n"
            f"Duration: {d['duration']}\n"
            f"Speed: {d['speed_mbps']} MB/s\n"
            f"Total time: {d['total_seconds']}s",
            chat_id=chat_id,
            message_id=status_message_id,
        )

    except requests.HTTPError as e:
        detail = e.response.json().get("detail", str(e))
        bot.edit_message_text(
            f"Upload failed: {detail}",
            chat_id=chat_id,
            message_id=status_message_id,
        )
    except requests.Timeout:
        bot.edit_message_text(
            "Upload timed out. The file may be too large or the server is busy.",
            chat_id=chat_id,
            message_id=status_message_id,
        )
    except Exception as e:
        logger.exception("Upload error")
        bot.edit_message_text(
            f"Unexpected error: {e}",
            chat_id=chat_id,
            message_id=status_message_id,
        )


# ── Run ────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    logger.info("Bot started. Polling...")
    bot.infinity_polling()
