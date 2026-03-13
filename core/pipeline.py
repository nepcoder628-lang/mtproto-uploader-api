"""
Video Upload Pipeline
=====================
Combines YouTubeDownloader + MTProtoUploader into a single easy-to-use
pipeline with live progress editing.

This is the main integration point for bot developers.

Usage example with python-telegram-bot:
    from mtproto_uploader import VideoUploadPipeline

    pipeline = VideoUploadPipeline(
        api_id=MY_API_ID,
        api_hash=MY_API_HASH,
        session_string=MY_SESSION,
    )

    await pipeline.start()

    # In your bot handler:
    result = await pipeline.process(
        url="https://youtu.be/...",
        chat_id=update.effective_chat.id,
        quality="720p",
        status_message_id=status_msg.message_id,  # optional: for live progress edits
        bot_token=BOT_TOKEN,                        # optional: for live progress edits
    )
"""

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Union

from core.downloader import YouTubeDownloader, VideoInfo
from core.uploader import MTProtoUploader, UploadProgress

logger = logging.getLogger(__name__)


@dataclass
class PipelineResult:
    """Result returned after a successful download + upload."""
    video_info: VideoInfo
    telegram_message_id: int
    chat_id: Union[int, str]
    upload_duration_seconds: float
    download_duration_seconds: float

    @property
    def total_duration_seconds(self) -> float:
        return self.upload_duration_seconds + self.download_duration_seconds

    @property
    def average_speed_mbps(self) -> float:
        if self.total_duration_seconds == 0:
            return 0.0
        return (self.video_info.filesize / 1_000_000) / self.total_duration_seconds


class ProgressEditor:
    """
    Edits a Telegram message to show live progress.
    Works with ANY bot framework via the Bot API HTTP endpoint.
    Uses aiohttp for async HTTP calls to avoid framework coupling.
    """

    THROTTLE_INTERVAL = 3.0  # Edit at most once every 3 seconds

    def __init__(
        self,
        bot_token: str,
        chat_id: Union[int, str],
        message_id: int,
    ):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.message_id = message_id
        self._last_edit = 0.0
        self._session = None

    async def _get_session(self):
        import aiohttp
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def edit(self, text: str, force: bool = False):
        """Edit the progress message. Throttled to avoid flood limits."""
        now = time.time()
        if not force and (now - self._last_edit) < self.THROTTLE_INTERVAL:
            return
        self._last_edit = now

        try:
            import aiohttp
            session = await self._get_session()
            url = f"https://api.telegram.org/bot{self.bot_token}/editMessageText"
            payload = {
                "chat_id": self.chat_id,
                "message_id": self.message_id,
                "text": text,
                "parse_mode": "HTML",
            }
            async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.debug(f"Edit message returned {resp.status}: {body[:200]}")
        except Exception as e:
            logger.debug(f"Progress edit failed (non-fatal): {e}")

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()


class VideoUploadPipeline:
    """
    Full pipeline: YouTube download → MTProto upload → live progress.

    Designed to be initialized once and reused across many requests.

    Args:
        api_id: Telegram API ID (from my.telegram.org)
        api_hash: Telegram API hash
        session_name: Local session name (for persistent auth)
        session_string: Exported session string (for stateless/server deploy)
        phone_number: Phone for user account login
        bot_token: Bot token (limited to ~50MB; use user account for large files)
        download_dir: Where to store temporary video files
        max_filesize_mb: Reject downloads larger than this
        auto_cleanup: Delete local file after successful upload
        cookies_file: Path to cookies.txt for age-restricted videos
    """

    def __init__(
        self,
        api_id: int,
        api_hash: str,
        session_name: str = "mtproto_uploader",
        session_string: Optional[str] = None,
        phone_number: Optional[str] = None,
        bot_token: Optional[str] = None,
        download_dir: str = "/tmp/mtproto_downloads",
        max_filesize_mb: int = 2000,
        auto_cleanup: bool = True,
        cookies_file: Optional[str] = None,
    ):
        self.auto_cleanup = auto_cleanup

        self._uploader = MTProtoUploader(
            api_id=api_id,
            api_hash=api_hash,
            session_name=session_name,
            session_string=session_string,
            phone_number=phone_number,
            bot_token=bot_token,
        )

        self._downloader = YouTubeDownloader(
            download_dir=download_dir,
            max_filesize_mb=max_filesize_mb,
            cookies_file=cookies_file,
        )

        self._started = False

    async def start(self):
        """Connect the MTProto client."""
        if not self._started:
            await self._uploader.start()
            self._started = True

    async def stop(self):
        """Disconnect."""
        if self._started:
            await self._uploader.stop()
            self._started = False

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, *args):
        await self.stop()

    async def export_session(self) -> str:
        """Export session string for future stateless runs."""
        return await self._uploader.export_session()

    async def get_video_info(self, url: str) -> VideoInfo:
        """Fetch video metadata without downloading."""
        return await self._downloader.get_info(url)

    async def get_available_qualities(self, url: str):
        """Return list of available quality options."""
        return await self._downloader.get_available_qualities(url)

    async def process(
        self,
        url: str,
        chat_id: Union[int, str],
        quality: str = "720p",
        caption: Optional[str] = None,
        reply_to_message_id: Optional[int] = None,
        # Live progress editing (optional)
        bot_token: Optional[str] = None,
        status_chat_id: Optional[Union[int, str]] = None,
        status_message_id: Optional[int] = None,
        # Custom progress callback (alternative to live editing)
        download_progress_callback: Optional[Callable] = None,
        upload_progress_callback: Optional[Callable] = None,
        progress_args: tuple = (),
        # Behaviour
        send_as_document: bool = False,
    ) -> PipelineResult:
        """
        Full pipeline: download video → upload via MTProto → send to chat.

        Args:
            url: YouTube / any supported video URL
            chat_id: Telegram chat to send the video to
            quality: Video quality preset (best/1080p/720p/480p/360p/worst/audio)
            caption: Caption for the message. If None, uses video title.
            reply_to_message_id: Reply to this message ID in the chat
            bot_token: Your bot token (for live progress editing)
            status_chat_id: Chat where the status message is (defaults to chat_id)
            status_message_id: Message ID to edit with live progress
            download_progress_callback: Called during download(current, total, *args)
            upload_progress_callback: Called during upload(current, total, *args)
            progress_args: Extra args for progress callbacks
            send_as_document: Send as raw document instead of video

        Returns:
            PipelineResult with all details about the operation
        """
        if not self._started:
            raise RuntimeError(
                "Pipeline not started. Call start() or use 'async with pipeline:'"
            )

        # Set up live progress editor if credentials provided
        editor: Optional[ProgressEditor] = None
        if bot_token and status_message_id:
            sc_id = status_chat_id or chat_id
            editor = ProgressEditor(
                bot_token=bot_token,
                chat_id=sc_id,
                message_id=status_message_id,
            )

        # ── PHASE 1: Download ──────────────────────────────────────────
        if editor:
            await editor.edit(
                f"⬇️ <b>Downloading...</b>\n"
                f"Quality: <code>{quality}</code>\n"
                f"URL: <code>{url[:60]}{'...' if len(url) > 60 else ''}</code>"
            )

        download_start = time.time()

        # Build download progress callback
        dl_progress_bar = UploadProgress("download", 0)

        async def _dl_progress(current: int, total: int, *args):
            dl_progress_bar.update(current, total)
            if editor:
                await editor.edit(
                    f"⬇️ <b>Downloading video...</b>\n"
                    f"{dl_progress_bar.bar()} {dl_progress_bar.percent:.1f}%\n"
                    f"📦 {current/1_000_000:.1f} / {total/1_000_000:.1f} MB\n"
                    f"⚡ {dl_progress_bar.speed_human}  |  ⏱ ETA: {dl_progress_bar.eta_human}"
                )
            if download_progress_callback:
                if asyncio.iscoroutinefunction(download_progress_callback):
                    await download_progress_callback(current, total, *progress_args)
                else:
                    download_progress_callback(current, total, *progress_args)

        video_info = await self._downloader.download(
            url=url,
            quality=quality,
            progress_callback=_dl_progress,
        )
        download_duration = time.time() - download_start

        if not video_info.local_path or not video_info.local_path.exists():
            raise RuntimeError("Download completed but file not found on disk.")

        # ── PHASE 2: Upload via MTProto ────────────────────────────────
        if editor:
            await editor.edit(
                f"⬆️ <b>Uploading via MTProto...</b>\n"
                f"📁 {video_info.title[:50]}\n"
                f"📦 {video_info.filesize_mb:.1f} MB  |  🎬 {video_info.duration_human}"
            )

        upload_start = time.time()

        # Build upload progress callback
        ul_progress_bar = UploadProgress(video_info.title, video_info.filesize)

        async def _ul_progress(current: int, total: int, *args):
            ul_progress_bar.update(current, total)
            if editor:
                await editor.edit(
                    f"⬆️ <b>Uploading to Telegram...</b>\n"
                    f"{ul_progress_bar.bar()} {ul_progress_bar.percent:.1f}%\n"
                    f"📦 {current/1_000_000:.1f} / {total/1_000_000:.1f} MB\n"
                    f"⚡ {ul_progress_bar.speed_human}  |  ⏱ ETA: {ul_progress_bar.eta_human}"
                )
            if upload_progress_callback:
                if asyncio.iscoroutinefunction(upload_progress_callback):
                    await upload_progress_callback(current, total, *progress_args)
                else:
                    upload_progress_callback(current, total, *progress_args)

        # Build caption
        final_caption = caption
        if final_caption is None:
            final_caption = (
                f"🎬 <b>{video_info.title}</b>\n"
                f"⏱ {video_info.duration_human}  |  📺 {video_info.resolution}\n"
                f"👁 {video_info.view_count:,} views"
            )

        if send_as_document:
            message = await self._uploader.send_document(
                chat_id=chat_id,
                file_path=video_info.local_path,
                caption=final_caption,
                progress_callback=_ul_progress,
                reply_to_message_id=reply_to_message_id,
            )
        else:
            message = await self._uploader.send_video(
                chat_id=chat_id,
                video_path=video_info.local_path,
                caption=final_caption,
                thumb=video_info.thumbnail_path,
                duration=video_info.duration,
                width=video_info.width,
                height=video_info.height,
                supports_streaming=True,
                progress_callback=_ul_progress,
                reply_to_message_id=reply_to_message_id,
            )

        upload_duration = time.time() - upload_start

        # ── PHASE 3: Cleanup ───────────────────────────────────────────
        if self.auto_cleanup:
            self._downloader.cleanup(video_info)

        if editor:
            await editor.edit(
                f"✅ <b>Done!</b>\n"
                f"📁 {video_info.title[:50]}\n"
                f"📦 {video_info.filesize_mb:.1f} MB in "
                f"{upload_duration:.0f}s",
                force=True,
            )
            await editor.close()

        return PipelineResult(
            video_info=video_info,
            telegram_message_id=message.id,
            chat_id=chat_id,
            upload_duration_seconds=upload_duration,
            download_duration_seconds=download_duration,
        )
