"""
MTProto Uploader REST API
=========================
A FastAPI server that exposes the MTProto upload pipeline as HTTP endpoints.

Any bot in any language can call this API to upload videos via MTProto.

Endpoints:
    POST /auth/send-code  - Step 1: send OTP to phone number
    POST /auth/verify     - Step 2: verify OTP → get session string
    POST /upload          - Download URL + upload to Telegram
    GET  /info            - Get video info (no download)
    GET  /qualities       - List available qualities for a URL
    GET  /session         - Export current session string
    GET  /health          - Health check

Start the server:
    uvicorn mtproto_uploader.api.server:app --host 0.0.0.0 --port 8080

Or via the CLI helper:
    python -m mtproto_uploader.api.server
"""

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, validator

from ..core.pipeline import VideoUploadPipeline, PipelineResult
from .auth import router as auth_router

logger = logging.getLogger(__name__)

# ── Global pipeline instance ───────────────────────────────────────────────
_pipeline: Optional[VideoUploadPipeline] = None


def get_pipeline() -> VideoUploadPipeline:
    if _pipeline is None:
        raise RuntimeError("Pipeline not initialized. Call init_pipeline() first.")
    return _pipeline


def init_pipeline(pipeline: VideoUploadPipeline):
    """Call this before starting the server."""
    global _pipeline
    _pipeline = pipeline


# ── Lifespan ───────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start/stop the MTProto client with the FastAPI app lifecycle."""
    global _pipeline

    # Auto-initialize from environment variables if not already set.
    # If API_ID/API_HASH are missing, skip pipeline init — the server still
    # starts so /auth/* endpoints are available for generating a session.
    if _pipeline is None:
        api_id = os.environ.get("API_ID")
        api_hash = os.environ.get("API_HASH")
        if api_id and api_hash:
            from ..core.pipeline import VideoUploadPipeline
            _pipeline = VideoUploadPipeline(
                api_id=int(api_id),
                api_hash=api_hash,
                session_string=os.environ.get("SESSION_STRING"),
                phone_number=os.environ.get("PHONE_NUMBER"),
                bot_token=os.environ.get("BOT_TOKEN"),
                download_dir=os.environ.get("DOWNLOAD_DIR", "/tmp/mtproto_downloads"),
                max_filesize_mb=int(os.environ.get("MAX_FILESIZE_MB", "2000")),
            )
            await _pipeline.start()
            logger.info("MTProto pipeline started.")
        else:
            logger.warning(
                "API_ID/API_HASH not set — upload endpoints disabled. "
                "Use POST /auth/send-code + /auth/verify to generate a SESSION_STRING first."
            )

    yield

    if _pipeline is not None:
        await _pipeline.stop()
        logger.info("MTProto pipeline stopped.")


# ── FastAPI app ────────────────────────────────────────────────────────────
app = FastAPI(
    title="MTProto Uploader API",
    description=(
        "Upload videos to Telegram via MTProto (up to 2GB/4GB). "
        "Integrate with any bot framework using simple HTTP calls. "
        "Use /auth/send-code + /auth/verify to generate a session string."
    ),
    version="1.0.0",
    lifespan=lifespan,
)

app.include_router(auth_router)


# ── Request / Response models ──────────────────────────────────────────────
class UploadRequest(BaseModel):
    url: str = Field(..., description="Video URL to download and upload")
    chat_id: str = Field(..., description="Target Telegram chat ID or @username")
    quality: str = Field("720p", description="Quality: best/1080p/720p/480p/360p/worst/audio")
    caption: Optional[str] = Field(None, description="Message caption. Uses video title if not set.")
    reply_to_message_id: Optional[int] = Field(None, description="Reply to this message ID")
    bot_token: Optional[str] = Field(None, description="Bot token for live progress message edits")
    status_chat_id: Optional[str] = Field(None, description="Chat ID of the status message")
    status_message_id: Optional[int] = Field(None, description="Message ID to edit with progress")
    send_as_document: bool = Field(False, description="Send as document instead of video")

    @validator("quality")
    def validate_quality(cls, v):
        valid = ["best", "1080p", "720p", "480p", "360p", "worst", "audio"]
        if v not in valid:
            raise ValueError(f"quality must be one of: {', '.join(valid)}")
        return v


class UploadResponse(BaseModel):
    success: bool
    message_id: int
    chat_id: str
    title: str
    filesize_mb: float
    duration_human: str
    resolution: str
    upload_seconds: float
    download_seconds: float
    total_seconds: float
    speed_mbps: float


class VideoInfoResponse(BaseModel):
    title: str
    url: str
    duration: int
    duration_human: str
    width: int
    height: int
    filesize_mb: float
    thumbnail_url: str
    uploader: str
    view_count: int
    description: str


class QualityOption(BaseModel):
    quality: str
    width: Optional[int]
    height: int
    fps: Optional[float]
    filesize_mb: Optional[float]


# ── Endpoints ──────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    """Health check."""
    return {"status": "ok", "pipeline_ready": _pipeline is not None and _pipeline._started}


@app.post("/upload", response_model=UploadResponse)
async def upload_video(req: UploadRequest):
    """
    Download a video from a URL and upload it to Telegram via MTProto.

    Example curl:
        curl -X POST http://localhost:8080/upload \\
          -H "Content-Type: application/json" \\
          -d '{
            "url": "https://youtu.be/dQw4w9WgXcQ",
            "chat_id": "123456789",
            "quality": "720p",
            "bot_token": "YOUR_BOT_TOKEN",
            "status_message_id": 42
          }'
    """
    pipeline = get_pipeline()

    try:
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

    return UploadResponse(
        success=True,
        message_id=result.telegram_message_id,
        chat_id=str(result.chat_id),
        title=result.video_info.title,
        filesize_mb=round(result.video_info.filesize_mb, 2),
        duration_human=result.video_info.duration_human,
        resolution=result.video_info.resolution,
        upload_seconds=round(result.upload_duration_seconds, 1),
        download_seconds=round(result.download_duration_seconds, 1),
        total_seconds=round(result.total_duration_seconds, 1),
        speed_mbps=round(result.average_speed_mbps, 2),
    )


@app.get("/info", response_model=VideoInfoResponse)
async def get_video_info(url: str):
    """
    Get video metadata without downloading.

    Example: GET /info?url=https://youtu.be/dQw4w9WgXcQ
    """
    pipeline = get_pipeline()
    try:
        info = await pipeline.get_video_info(url)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    return VideoInfoResponse(
        title=info.title,
        url=info.url,
        duration=info.duration,
        duration_human=info.duration_human,
        width=info.width,
        height=info.height,
        filesize_mb=round(info.filesize_mb, 2),
        thumbnail_url=info.thumbnail_url,
        uploader=info.uploader,
        view_count=info.view_count,
        description=info.description,
    )


@app.get("/qualities")
async def get_qualities(url: str):
    """
    List available quality options for a video.

    Example: GET /qualities?url=https://youtu.be/dQw4w9WgXcQ
    """
    pipeline = get_pipeline()
    try:
        qualities = await pipeline.get_available_qualities(url)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {
        "url": url,
        "qualities": [
            {
                "quality": f"{q['height']}p",
                "width": q.get("width"),
                "height": q["height"],
                "fps": q.get("fps"),
                "filesize_mb": round(q["filesize"] / 1e6, 1) if q.get("filesize") else None,
            }
            for q in qualities
        ]
    }


@app.get("/session")
async def export_session():
    """
    Export the current session string.
    Save this and use it as SESSION_STRING environment variable on future runs.
    """
    pipeline = get_pipeline()
    try:
        session = await pipeline.export_session()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"session_string": session}


# ── Utility ────────────────────────────────────────────────────────────────
def _parse_chat_id(value: str) -> int | str:
    """Convert string to int if it's a numeric chat ID."""
    try:
        return int(value)
    except (ValueError, TypeError):
        return value


# ── Standalone entry point ─────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn

    logging.basicConfig(level=logging.INFO)

    # The lifespan handler auto-reads env vars (API_ID, API_HASH, SESSION_STRING, etc.)
    # Just start uvicorn — no manual pipeline init needed.
    uvicorn.run(
        "mtproto_uploader.api.server:app",
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", 8080)),
        reload=False,
    )
