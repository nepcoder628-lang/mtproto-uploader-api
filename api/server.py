"""
MTProto Uploader REST API
=========================
Fully stateless — every request carries its own Telegram credentials.
No environment variables required. Multiple users can use the same
hosted instance with their own api_id / api_hash / session_string.

Endpoints:
    POST /auth/send-code  - Step 1: send OTP to phone
    POST /auth/verify     - Step 2: verify OTP → get session_string
    POST /upload          - Download (via prexzy/yt) + upload to Telegram
    POST /direct          - Download any direct URL + upload to Telegram
    POST /info            - Get video metadata (no download)
    POST /qualities       - List available qualities for a URL
    GET  /health          - Health check

All endpoints (except /auth/* and /health) require:
    api_id, api_hash, session_string  in the JSON body.
"""

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, validator

from core.downloader import YouTubeDownloader, VideoInfo
from core.pipeline import VideoUploadPipeline, PipelineResult
from api.auth import router as auth_router

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


# ── Lifespan (stateless — nothing to initialise) ────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    yield


# ── FastAPI app ────────────────────────────────────────────────────────────
app = FastAPI(
    title="MTProto Uploader API",
    description=(
        "Upload videos up to 2GB to Telegram via MTProto. "
        "Fully multi-user — pass your own api_id, api_hash, session_string "
        "in every request. Use /auth/send-code + /auth/verify to get a session_string."
    ),
    version="2.0.0",
    lifespan=lifespan,
)

app.include_router(auth_router)


# ── Shared credential fields (mixed into every request body) ───────────────
class TelegramCreds(BaseModel):
    api_id: int = Field(..., description="From https://my.telegram.org")
    api_hash: str = Field(..., description="From https://my.telegram.org")
    session_string: str = Field(..., description="From POST /auth/verify")


# ── Request / Response models ──────────────────────────────────────────────
class UploadRequest(TelegramCreds):
    url: str = Field(..., description="Video URL (YouTube, TikTok, etc.)")
    chat_id: str = Field(..., description="Target Telegram chat ID or @username")
    quality: str = Field("720p", description="best / 1080p / 720p / 360p / 144p / audio")
    caption: Optional[str] = Field(None, description="Message caption. Defaults to video title.")
    reply_to_message_id: Optional[int] = Field(None)
    bot_token: Optional[str] = Field(None, description="Bot token for live progress message edits")
    status_chat_id: Optional[str] = Field(None)
    status_message_id: Optional[int] = Field(None)
    send_as_document: bool = Field(False)

    @validator("quality")
    def validate_quality(cls, v):
        valid = ["best", "1080p", "720p", "480p", "360p", "240p", "144p", "worst", "audio"]
        if v not in valid:
            raise ValueError(f"quality must be one of: {', '.join(valid)}")
        return v


class DirectUploadRequest(TelegramCreds):
    url: str = Field(..., description="Direct video/mp4 URL to download and upload")
    chat_id: str = Field(..., description="Target Telegram chat ID or @username")
    caption: Optional[str] = Field(None, description="Message caption")
    reply_to_message_id: Optional[int] = Field(None)
    bot_token: Optional[str] = Field(None)
    status_chat_id: Optional[str] = Field(None)
    status_message_id: Optional[int] = Field(None)
    send_as_document: bool = Field(False)


class InfoRequest(TelegramCreds):
    url: str = Field(..., description="Video URL")


class QualitiesRequest(TelegramCreds):
    url: str = Field(..., description="Video URL")


# ── Helper ─────────────────────────────────────────────────────────────────
def _parse_chat_id(value: str) -> int | str:
    try:
        return int(value)
    except (ValueError, TypeError):
        return value


def _make_pipeline(creds: TelegramCreds) -> VideoUploadPipeline:
    return VideoUploadPipeline(
        api_id=creds.api_id,
        api_hash=creds.api_hash,
        session_string=creds.session_string,
        download_dir="/tmp/mtproto_downloads",
        max_filesize_mb=2000,
        auto_cleanup=True,
    )


def _make_downloader() -> YouTubeDownloader:
    return YouTubeDownloader(download_dir="/tmp/mtproto_downloads")


# ── Endpoints ──────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/upload")
async def upload_video(req: UploadRequest):
    """
    Download a video and upload it to Telegram via MTProto.

    Example:
        curl -X POST https://your-api.onrender.com/upload \\
          -H "Content-Type: application/json" \\
          -d '{
            "api_id": 12345678,
            "api_hash": "abc...",
            "session_string": "BQA...",
            "url": "https://youtu.be/dQw4w9WgXcQ",
            "chat_id": "123456789",
            "quality": "720p"
          }'
    """
    pipeline = _make_pipeline(req)
    try:
        async with pipeline:
            result: PipelineResult = await pipeline.process(
                url=req.url,
                chat_id=_parse_chat_id(req.chat_id),
                quality=req.quality,
                caption=req.caption,
                reply_to_message_id=req.reply_to_message_id,
                bot_token=req.bot_token,
                status_chat_id=_parse_chat_id(req.status_chat_id) if req.status_chat_id else None,
                status_message_id=req.status_message_id,
                send_as_document=req.send_as_document,
            )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.exception("Upload failed")
        raise HTTPException(status_code=500, detail=str(e))

    return {
        "success": True,
        "message_id": result.telegram_message_id,
        "chat_id": str(result.chat_id),
        "title": result.video_info.title,
        "filesize_mb": round(result.video_info.filesize_mb, 2),
        "duration": result.video_info.duration_human,
        "resolution": result.video_info.resolution,
        "download_seconds": round(result.download_duration_seconds, 1),
        "upload_seconds": round(result.upload_duration_seconds, 1),
        "total_seconds": round(result.total_duration_seconds, 1),
        "speed_mbps": round(result.average_speed_mbps, 2),
    }


@app.post("/info")
async def get_video_info(req: InfoRequest):
    """
    Get video metadata without downloading anything.

    Example:
        curl -X POST https://your-api.onrender.com/info \\
          -H "Content-Type: application/json" \\
          -d '{
            "api_id": 12345678,
            "api_hash": "abc...",
            "session_string": "BQA...",
            "url": "https://youtu.be/dQw4w9WgXcQ"
          }'
    """
    dl = YouTubeDownloader(download_dir="/tmp/mtproto_downloads")
    try:
        info: VideoInfo = await dl.get_info(req.url)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {
        "title": info.title,
        "url": info.url,
        "duration": info.duration,
        "duration_human": info.duration_human,
        "width": info.width,
        "height": info.height,
        "filesize_mb": round(info.filesize_mb, 2),
        "thumbnail_url": info.thumbnail_url,
        "uploader": info.uploader,
        "view_count": info.view_count,
        "description": info.description,
    }


@app.post("/qualities")
async def get_qualities(req: QualitiesRequest):
    """
    List available quality options for a video URL.

    Example:
        curl -X POST https://your-api.onrender.com/qualities \\
          -H "Content-Type: application/json" \\
          -d '{
            "api_id": 12345678,
            "api_hash": "abc...",
            "session_string": "BQA...",
            "url": "https://youtu.be/dQw4w9WgXcQ"
          }'
    """
    dl = YouTubeDownloader(download_dir="/tmp/mtproto_downloads")
    try:
        qualities = await dl.get_available_qualities(req.url)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {
        "url": req.url,
        "qualities": [
            {
                "quality": f"{q['height']}p",
                "width": q.get("width"),
                "height": q["height"],
                "fps": q.get("fps"),
                "filesize_mb": round(q["filesize"] / 1e6, 1) if q.get("filesize") else None,
            }
            for q in qualities
        ],
    }


@app.post("/direct")
async def direct_upload(req: DirectUploadRequest):
    """
    Download any direct video/mp4 URL via aiohttp and upload to Telegram via MTProto.
    No yt-dlp, no prexzy — just raw HTTP download + MTProto upload.

    Example:
        curl -X POST https://your-api.onrender.com/direct \\
          -H "Content-Type: application/json" \\
          -d '{
            "api_id": 12345678,
            "api_hash": "abc...",
            "session_string": "BQA...",
            "url": "https://example.com/video.mp4",
            "chat_id": "123456789"
          }'
    """
    import aiohttp as _aiohttp
    import re as _re
    import time as _time

    download_dir = Path("/tmp/mtproto_downloads")
    download_dir.mkdir(parents=True, exist_ok=True)

    # Derive a safe filename from the URL
    url_path = req.url.split("?")[0].rstrip("/")
    raw_name = url_path.split("/")[-1] or "video"
    safe_name = _re.sub(r'[^\w\-.]', '_', raw_name)[:80]
    if not safe_name.endswith((".mp4", ".mkv", ".webm", ".mov", ".avi")):
        safe_name += ".mp4"
    local_path = download_dir / safe_name

    # ── Download ────────────────────────────────────────────────────────────
    dl_start = _time.time()

    # Extract origin (scheme + host) for Referer — many CDNs require Referer
    # to match the file's own domain (hotlink protection).
    from urllib.parse import urlparse as _urlparse
    _parsed = _urlparse(req.url)
    _origin = f"{_parsed.scheme}://{_parsed.netloc}"

    _headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "video/webm,video/mp4,video/*;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "identity",   # avoid compressed responses for binary files
        "Referer": _origin + "/",        # domain root — satisfies hotlink checks
        "Origin": _origin,
        "Connection": "keep-alive",
        "Sec-Fetch-Dest": "video",
        "Sec-Fetch-Mode": "no-cors",
        "Sec-Fetch-Site": "same-origin",
    }
    try:
        async with _aiohttp.ClientSession(headers=_headers) as session:
            async with session.get(
                req.url,
                timeout=_aiohttp.ClientTimeout(total=3600),
                allow_redirects=True,
            ) as resp:
                if resp.status == 403:
                    # Some hosts block even with correct Referer from server IPs.
                    # Try again without Referer/Origin as last resort.
                    pass
                else:
                    resp.raise_for_status()
                    with open(local_path, "wb") as f:
                        async for chunk in resp.content.iter_chunked(256 * 1024):
                            f.write(chunk)

        # If 403, retry without hotlink headers (some servers block Origin header)
        if resp.status == 403:
            _bare_headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                "Accept": "*/*",
                "Accept-Encoding": "identity",
            }
            async with _aiohttp.ClientSession(headers=_bare_headers) as session2:
                async with session2.get(
                    req.url,
                    timeout=_aiohttp.ClientTimeout(total=3600),
                    allow_redirects=True,
                ) as resp2:
                    resp2.raise_for_status()
                    with open(local_path, "wb") as f:
                        async for chunk in resp2.content.iter_chunked(256 * 1024):
                            f.write(chunk)

    except _aiohttp.ClientResponseError as e:
        raise HTTPException(
            status_code=400,
            detail=f"Download failed ({e.status}): server rejected the request. "
                   f"The URL may require authentication or block server IPs."
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Download failed: {e}")

    dl_duration = _time.time() - dl_start
    filesize = local_path.stat().st_size

    # ── Upload via MTProto ───────────────────────────────────────────────────
    from core.uploader import MTProtoUploader

    uploader = MTProtoUploader(
        api_id=req.api_id,
        api_hash=req.api_hash,
        session_string=req.session_string,
    )

    ul_start = _time.time()
    try:
        await uploader.start()
        caption = req.caption or safe_name

        if req.send_as_document:
            message = await uploader.send_document(
                chat_id=_parse_chat_id(req.chat_id),
                file_path=local_path,
                caption=caption,
                reply_to_message_id=req.reply_to_message_id,
            )
        else:
            message = await uploader.send_video(
                chat_id=_parse_chat_id(req.chat_id),
                video_path=local_path,
                caption=caption,
                supports_streaming=True,
                reply_to_message_id=req.reply_to_message_id,
            )
    except Exception as e:
        logger.exception("MTProto upload failed")
        raise HTTPException(status_code=500, detail=f"Upload failed: {e}")
    finally:
        await uploader.stop()
        try:
            local_path.unlink()
        except Exception:
            pass

    ul_duration = _time.time() - ul_start
    total = dl_duration + ul_duration

    return {
        "success": True,
        "message_id": message.id,
        "chat_id": str(req.chat_id),
        "filename": safe_name,
        "filesize_mb": round(filesize / 1_000_000, 2),
        "download_seconds": round(dl_duration, 1),
        "upload_seconds": round(ul_duration, 1),
        "total_seconds": round(total, 1),
        "speed_mbps": round((filesize / 1_000_000) / total if total else 0, 2),
    }


# ── Standalone entry point ─────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "mtproto_uploader.api.server:app",
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", 8080)),
        reload=False,
    )
