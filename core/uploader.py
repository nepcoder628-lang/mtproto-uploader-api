"""
MTProto Uploader Core
=====================
Uses Pyrogram (MTProto) to upload videos up to 2GB (4GB with Premium).

MTProto Upload Protocol (from Telegram docs):
- Files < 10MB  → upload.saveFilePart   + inputFile
- Files >= 10MB → upload.saveBigFilePart + inputFileBig
- Part size: must be divisible by 1024, and 524288 (512KB) must be divisible by part_size
- Recommended part_size = 512KB (524288 bytes) for maximum throughput
- Max parts (free): 4000 → max 4000 * 512KB = ~2GB
- Max parts (premium): 8000 → max 8000 * 512KB = ~4GB
- Upload can be parallelised across multiple TCP connections for speed

This module wraps Pyrogram's built-in MTProto upload so you don't need
to implement the protocol by hand.
"""

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Callable, Optional, Union

import pyrogram
from pyrogram import Client
from pyrogram.types import Message

logger = logging.getLogger(__name__)


class UploadProgress:
    """Tracks upload progress and computes speed/ETA."""

    def __init__(self, filename: str, total_size: int):
        self.filename = filename
        self.total_size = total_size
        self.current = 0
        self.start_time = time.time()
        self.last_update_time = 0.0
        self.speed_bps = 0.0

    def update(self, current: int, total: int):
        self.current = current
        self.total_size = total
        elapsed = time.time() - self.start_time
        if elapsed > 0:
            self.speed_bps = current / elapsed

    @property
    def percent(self) -> float:
        if self.total_size == 0:
            return 0.0
        return (self.current / self.total_size) * 100

    @property
    def speed_human(self) -> str:
        bps = self.speed_bps
        if bps >= 1_000_000:
            return f"{bps/1_000_000:.1f} MB/s"
        elif bps >= 1_000:
            return f"{bps/1_000:.1f} KB/s"
        return f"{bps:.0f} B/s"

    @property
    def eta_seconds(self) -> float:
        remaining = self.total_size - self.current
        if self.speed_bps > 0:
            return remaining / self.speed_bps
        return 0.0

    @property
    def eta_human(self) -> str:
        eta = int(self.eta_seconds)
        minutes, seconds = divmod(eta, 60)
        hours, minutes = divmod(minutes, 60)
        if hours:
            return f"{hours}h {minutes}m {seconds}s"
        if minutes:
            return f"{minutes}m {seconds}s"
        return f"{seconds}s"

    def bar(self, width: int = 20) -> str:
        filled = int(self.percent / 100 * width)
        empty = width - filled
        return "█" * filled + "░" * empty


class MTProtoUploader:
    """
    Standalone MTProto upload client using Pyrogram.

    Can be integrated with ANY Telegram bot framework:
    - python-telegram-bot
    - telebot (pyTelegramBotAPI)
    - aiogram
    - Manual Bot API

    Usage:
        uploader = MTProtoUploader(
            api_id=12345,
            api_hash="your_api_hash",
            session_string="...optional saved session...",
            # OR phone_number="+1234567890" for first-time login
        )

        async with uploader:
            message = await uploader.send_video(
                chat_id=123456789,
                video_path="/path/to/video.mp4",
                caption="My video",
                progress_callback=my_callback,
            )
    """

    # Telegram limits
    FREE_MAX_SIZE = 2 * 1024 * 1024 * 1024   # 2 GB
    PREMIUM_MAX_SIZE = 4 * 1024 * 1024 * 1024  # 4 GB
    PART_SIZE = 512 * 1024  # 512 KB — recommended by Telegram for best performance

    def __init__(
        self,
        api_id: int,
        api_hash: str,
        session_name: str = "mtproto_uploader",
        session_string: Optional[str] = None,
        phone_number: Optional[str] = None,
        bot_token: Optional[str] = None,
        workdir: str = ".",
        max_concurrent_transmissions: int = 1,
    ):
        """
        Args:
            api_id: From https://my.telegram.org
            api_hash: From https://my.telegram.org
            session_name: Local session file name (used if no session_string)
            session_string: Exported session string (for stateless/server deploys)
            phone_number: Phone number for user account login
            bot_token: Bot token (note: bot accounts cannot upload >50MB via Bot API;
                       use a user account for large files)
            workdir: Where to store session files
            max_concurrent_transmissions: Number of parallel upload connections (1-10)
        """
        self.api_id = api_id
        self.api_hash = api_hash
        self.session_string = session_string
        self.session_name = session_name
        self.phone_number = phone_number
        self.bot_token = bot_token
        self.workdir = workdir
        self.max_concurrent_transmissions = max_concurrent_transmissions

        self._client: Optional[Client] = None
        self._started = False

    def _build_client(self) -> Client:
        """Construct the Pyrogram client based on provided credentials."""
        kwargs = dict(
            api_id=self.api_id,
            api_hash=self.api_hash,
            workdir=self.workdir,
        )

        if self.session_string:
            # Stateless: use exported session string (best for servers/Docker)
            return Client(
                name=":memory:",
                session_string=self.session_string,
                **kwargs,
            )
        elif self.bot_token:
            # Bot account — limited to ~50MB for file uploads
            logger.warning(
                "Bot account login: MTProto upload works but Telegram "
                "limits bots to ~50MB. Use a user account for larger files."
            )
            return Client(
                name=self.session_name,
                bot_token=self.bot_token,
                **kwargs,
            )
        else:
            # User account — supports up to 2GB (4GB with Premium)
            return Client(
                name=self.session_name,
                phone_number=self.phone_number,
                **kwargs,
            )

    async def start(self):
        """Start the Pyrogram client (connect + auth)."""
        if self._started:
            return
        self._client = self._build_client()
        await self._client.start()
        self._started = True
        me = await self._client.get_me()
        logger.info(f"MTProtoUploader started as: {me.first_name} (id={me.id})")

    async def stop(self):
        """Disconnect the Pyrogram client."""
        if self._client and self._started:
            await self._client.stop()
            self._started = False

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, *args):
        await self.stop()

    def _validate_file(self, path: Union[str, Path]) -> Path:
        """Validate file exists and size is within Telegram limits."""
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"File not found: {p}")
        size = p.stat().st_size
        if size == 0:
            raise ValueError(f"File is empty: {p}")
        if size > self.PREMIUM_MAX_SIZE:
            raise ValueError(
                f"File too large: {size / 1e9:.2f}GB. "
                f"Telegram max is 4GB (with Premium)."
            )
        if size > self.FREE_MAX_SIZE:
            logger.warning(
                f"File is {size / 1e9:.2f}GB — requires Telegram Premium on the account."
            )
        return p

    async def export_session(self) -> str:
        """
        Export the current session as a string.
        Save this and use it as `session_string` on future runs
        (avoids re-authentication).
        """
        if not self._client:
            raise RuntimeError("Client not started. Call start() first.")
        return await self._client.export_session_string()

    # ------------------------------------------------------------------
    # Core upload methods
    # ------------------------------------------------------------------

    async def send_video(
        self,
        chat_id: Union[int, str],
        video_path: Union[str, Path],
        caption: str = "",
        supports_streaming: bool = True,
        thumb: Optional[Union[str, Path]] = None,
        duration: Optional[int] = None,
        width: Optional[int] = None,
        height: Optional[int] = None,
        progress_callback: Optional[Callable] = None,
        progress_args: tuple = (),
        reply_to_message_id: Optional[int] = None,
    ) -> Message:
        """
        Upload a video file via MTProto and send it to a chat.

        Args:
            chat_id: Target chat (int id or @username).
            video_path: Path to the .mp4 / .mkv / .webm file.
            caption: Message caption (HTML/Markdown supported).
            supports_streaming: Set True to enable in-app streaming.
            thumb: Path to thumbnail image (.jpg, max 200KB, 320x320).
            duration: Video duration in seconds.
            width, height: Video dimensions.
            progress_callback: async or sync callable(current, total, *progress_args).
                               Called every ~512KB of upload.
            progress_args: Extra args passed to progress_callback.
            reply_to_message_id: Reply to a specific message.

        Returns:
            pyrogram.types.Message — the sent message.

        Example:
            async def on_progress(current, total):
                print(f"{current*100/total:.1f}%")

            msg = await uploader.send_video(
                chat_id=123456,
                video_path="video.mp4",
                caption="Check this out!",
                progress_callback=on_progress,
            )
        """
        if not self._started:
            raise RuntimeError("Call start() or use 'async with MTProtoUploader(...)'")

        video_path = self._validate_file(video_path)
        file_size = video_path.stat().st_size
        logger.info(
            f"Uploading {video_path.name} "
            f"({file_size/1_000_000:.1f} MB) to chat {chat_id}"
        )

        # Build progress wrapper that injects progress_args
        async def _progress(current: int, total: int):
            if progress_callback:
                if asyncio.iscoroutinefunction(progress_callback):
                    await progress_callback(current, total, *progress_args)
                else:
                    progress_callback(current, total, *progress_args)

        kwargs = dict(
            chat_id=chat_id,
            video=str(video_path),
            caption=caption,
            supports_streaming=supports_streaming,
            progress=_progress,
        )

        if thumb:
            kwargs["thumb"] = str(thumb)
        if duration is not None:
            kwargs["duration"] = duration
        if width is not None:
            kwargs["width"] = width
        if height is not None:
            kwargs["height"] = height
        if reply_to_message_id is not None:
            kwargs["reply_to_message_id"] = reply_to_message_id

        message = await self._client.send_video(**kwargs)
        logger.info(f"Upload complete. Message id: {message.id}")
        return message

    async def send_document(
        self,
        chat_id: Union[int, str],
        file_path: Union[str, Path],
        caption: str = "",
        progress_callback: Optional[Callable] = None,
        progress_args: tuple = (),
        reply_to_message_id: Optional[int] = None,
    ) -> Message:
        """
        Send any file as a document via MTProto (no size/codec restrictions).
        Use this for raw video files that don't need streaming.
        """
        if not self._started:
            raise RuntimeError("Client not started.")

        file_path = self._validate_file(file_path)

        async def _progress(current: int, total: int):
            if progress_callback:
                if asyncio.iscoroutinefunction(progress_callback):
                    await progress_callback(current, total, *progress_args)
                else:
                    progress_callback(current, total, *progress_args)

        kwargs = dict(
            chat_id=chat_id,
            document=str(file_path),
            caption=caption,
            progress=_progress,
        )
        if reply_to_message_id is not None:
            kwargs["reply_to_message_id"] = reply_to_message_id

        message = await self._client.send_document(**kwargs)
        logger.info(f"Document upload complete. Message id: {message.id}")
        return message

    async def upload_file_only(
        self,
        file_path: Union[str, Path],
        progress_callback: Optional[Callable] = None,
        progress_args: tuple = (),
    ):
        """
        Upload a file to Telegram's servers WITHOUT sending it to any chat.
        Returns the InputFile object that can be reused to send to multiple chats
        without re-uploading.

        This is useful when:
        - You want to upload once and forward to many chats
        - You want to pre-upload before deciding where to send

        Returns:
            pyrogram raw InputFile object
        """
        if not self._started:
            raise RuntimeError("Client not started.")

        file_path = self._validate_file(file_path)

        async def _progress(current: int, total: int):
            if progress_callback:
                if asyncio.iscoroutinefunction(progress_callback):
                    await progress_callback(current, total, *progress_args)
                else:
                    progress_callback(current, total, *progress_args)

        return await self._client.save_file(str(file_path), progress=_progress)
