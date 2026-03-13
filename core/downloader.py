"""
Video Downloader
================
Uses the AceThinker API to resolve direct download URLs for YouTube videos,
then downloads them directly — no yt-dlp, no bot detection issues.

For video-only formats (1080p / 720p), audio is merged using ffmpeg.
For muxed formats (360p and below), no merge is needed.

Supports quality presets: best, 1080p, 720p, 480p, 360p, worst, audio
"""

import asyncio
import logging
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Union

import aiohttp
import requests

logger = logging.getLogger(__name__)

ACETHINKER_API = "https://www.acethinker.ai/downloader/api/dlapinewv2.php"

# Quality preference order for video streams (height)
QUALITY_MAP: Dict[str, Optional[int]] = {
    "best":  None,    # highest available
    "1080p": 1080,
    "720p":  720,
    "480p":  480,
    "360p":  360,
    "144p":  144,
    "worst": 0,       # lowest available
    "audio": -1,      # audio only
}


@dataclass
class VideoInfo:
    """Metadata about a video."""
    title: str
    url: str
    duration: int           # seconds
    width: int
    height: int
    ext: str
    filesize: int           # bytes (0 if unknown)
    thumbnail_url: str
    uploader: str
    view_count: int
    description: str
    local_path: Optional[Path] = None
    thumbnail_path: Optional[Path] = None

    @property
    def filesize_mb(self) -> float:
        return self.filesize / 1_000_000

    @property
    def resolution(self) -> str:
        return f"{self.width}x{self.height}"

    @property
    def duration_human(self) -> str:
        m, s = divmod(self.duration, 60)
        h, m = divmod(m, 60)
        if h:
            return f"{h}:{m:02d}:{s:02d}"
        return f"{m}:{s:02d}"


class YouTubeDownloader:
    """
    Download YouTube videos via AceThinker API + direct HTTP download.

    Quality presets:
        "best"    → highest available video quality
        "1080p"   → 1080p video (merged with audio via ffmpeg)
        "720p"    → 720p video (merged with audio via ffmpeg)
        "480p"    → 480p video if available, else next best
        "360p"    → 360p muxed (video+audio, no merge needed)
        "worst"   → lowest quality
        "audio"   → audio only (m4a)
    """

    def __init__(
        self,
        download_dir: Union[str, Path] = "/tmp/mtproto_downloads",
        max_filesize_mb: int = 2000,
        cookies_file: Optional[str] = None,  # kept for API compat, unused
    ):
        self.download_dir = Path(download_dir)
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.max_filesize_mb = max_filesize_mb

    # ── AceThinker API ──────────────────────────────────────────────────────

    def _fetch_formats(self, url: str) -> dict:
        """Call AceThinker API and return parsed response dict."""
        resp = requests.get(ACETHINKER_API, params={"url": url}, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if data.get("message") != "success":
            raise RuntimeError(f"AceThinker API error: {data}")
        return data["res_data"]

    def _select_video_format(self, formats: list, quality: str) -> Optional[dict]:
        """Pick the best video format for the requested quality."""
        # Video-only and muxed streams
        video_fmts = [
            f for f in formats
            if f.get("vcodec") and f["vcodec"] != "none"
            and f.get("ext") in ("mp4", "webm")
        ]
        if not video_fmts:
            return None

        target_height = QUALITY_MAP.get(quality)

        if target_height is None:
            # best: highest height
            return max(video_fmts, key=lambda f: self._height(f))
        elif target_height == 0:
            # worst: lowest height
            return min(video_fmts, key=lambda f: self._height(f))
        else:
            # exact or closest without exceeding
            under = [f for f in video_fmts if self._height(f) <= target_height]
            if under:
                return max(under, key=lambda f: self._height(f))
            # all are higher — take the lowest available
            return min(video_fmts, key=lambda f: self._height(f))

    def _select_audio_format(self, formats: list) -> Optional[dict]:
        """Pick the best audio-only format."""
        audio_fmts = [
            f for f in formats
            if (f.get("vcodec") == "none" or not f.get("vcodec"))
            and f.get("acodec") and f["acodec"] != "none"
            and f.get("ext") in ("m4a", "weba", "webm")
        ]
        if not audio_fmts:
            return None
        # Prefer m4a (better compat), then largest filesize
        m4a = [f for f in audio_fmts if f.get("ext") == "m4a"]
        pool = m4a if m4a else audio_fmts
        return max(pool, key=lambda f: f.get("filesize") or 0)

    def _height(self, fmt: dict) -> int:
        q = fmt.get("quality", "0p")
        try:
            return int(q.replace("p", ""))
        except ValueError:
            return 0

    def _is_muxed(self, fmt: dict) -> bool:
        """True if this format has both video and audio."""
        return (
            fmt.get("vcodec") not in (None, "none", "")
            and fmt.get("acodec") not in (None, "none", "")
        )

    # ── Public interface ────────────────────────────────────────────────────

    async def get_info(self, url: str) -> VideoInfo:
        """Fetch video metadata without downloading."""
        loop = asyncio.get_event_loop()
        data = await loop.run_in_executor(None, self._fetch_formats, url)
        return self._parse_info(data, url)

    def _parse_info(self, data: dict, url: str) -> VideoInfo:
        formats = data.get("formats", [])
        video_fmts = [f for f in formats if f.get("vcodec") and f["vcodec"] != "none"]
        best = max(video_fmts, key=lambda f: self._height(f)) if video_fmts else None
        height = self._height(best) if best else 0

        return VideoInfo(
            title=data.get("title", "Unknown"),
            url=url,
            duration=int(data.get("duration") or 0),
            width=0,        # AceThinker doesn't return width
            height=height,
            ext="mp4",
            filesize=int(best.get("filesize") or 0) if best else 0,
            thumbnail_url=data.get("thumbnail", ""),
            uploader=data.get("source", "youtube"),
            view_count=0,
            description="",
        )

    async def get_available_qualities(self, url: str) -> List[Dict]:
        """List available quality options."""
        loop = asyncio.get_event_loop()
        data = await loop.run_in_executor(None, self._fetch_formats, url)
        formats = data.get("formats", [])

        seen = set()
        result = []
        for f in formats:
            if f.get("vcodec") in (None, "none", ""):
                continue
            q = f.get("quality", "")
            if q in seen:
                continue
            seen.add(q)
            h = self._height(f)
            result.append({
                "quality": q,
                "width": 0,
                "height": h,
                "fps": None,
                "filesize": f.get("filesize"),
                "format_id": f.get("ext"),
            })

        return sorted(result, key=lambda x: x["height"], reverse=True)

    async def download(
        self,
        url: str,
        quality: str = "720p",
        output_filename: Optional[str] = None,
        progress_callback: Optional[Callable] = None,
        progress_args: tuple = (),
    ) -> VideoInfo:
        """
        Download a video using AceThinker API for URL resolution.

        For video-only formats (720p, 1080p), merges with best audio via ffmpeg.
        For muxed formats (360p), downloads directly.
        """
        loop = asyncio.get_event_loop()
        data = await loop.run_in_executor(None, self._fetch_formats, url)
        formats = data.get("formats", [])
        info = self._parse_info(data, url)

        if quality == "audio":
            audio_fmt = self._select_audio_format(formats)
            if not audio_fmt:
                raise ValueError("No audio stream available for this video.")
            out_path = self.download_dir / self._safe_name(info.title, "m4a")
            await self._download_file(
                audio_fmt["url"], out_path, progress_callback, progress_args
            )
            info.local_path = out_path
            info.filesize = out_path.stat().st_size
            return info

        video_fmt = self._select_video_format(formats, quality)
        if not video_fmt:
            raise ValueError(f"No video stream available for quality '{quality}'.")

        if self._is_muxed(video_fmt):
            # Already has audio — download directly
            ext = video_fmt.get("ext", "mp4")
            out_path = self.download_dir / self._safe_name(info.title, ext)
            await self._download_file(
                video_fmt["url"], out_path, progress_callback, progress_args
            )
            info.local_path = out_path
            info.height = self._height(video_fmt)
            info.filesize = out_path.stat().st_size

        else:
            # Video-only — need to merge with audio via ffmpeg
            audio_fmt = self._select_audio_format(formats)
            if not audio_fmt:
                raise ValueError("No audio stream available to merge with video.")

            # Use temp files for video and audio parts
            video_tmp = self.download_dir / self._safe_name(info.title + "_video", video_fmt.get("ext", "mp4"))
            audio_tmp = self.download_dir / self._safe_name(info.title + "_audio", audio_fmt.get("ext", "m4a"))
            out_path = self.download_dir / self._safe_name(info.title, "mp4")

            logger.info(f"Downloading video stream ({video_fmt.get('quality')})...")
            await self._download_file(
                video_fmt["url"], video_tmp, progress_callback, progress_args
            )

            logger.info("Downloading audio stream...")
            await self._download_file(audio_fmt["url"], audio_tmp, None, ())

            logger.info("Merging video + audio with ffmpeg...")
            await loop.run_in_executor(
                None, self._ffmpeg_merge, video_tmp, audio_tmp, out_path
            )

            # Clean up temp parts
            for p in [video_tmp, audio_tmp]:
                try:
                    p.unlink()
                except Exception:
                    pass

            info.local_path = out_path
            info.height = self._height(video_fmt)
            info.filesize = out_path.stat().st_size if out_path.exists() else 0

        # Download thumbnail
        info.thumbnail_path = await self._download_thumbnail(
            data.get("thumbnail", ""), info.title
        )

        logger.info(
            f"Download complete: {info.local_path.name if info.local_path else '?'} "
            f"({info.filesize_mb:.1f} MB)"
        )
        return info

    # ── Internals ───────────────────────────────────────────────────────────

    async def _download_file(
        self,
        url: str,
        dest: Path,
        progress_callback: Optional[Callable],
        progress_args: tuple,
    ):
        """Stream-download url → dest with optional progress reporting."""
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=3600)) as resp:
                resp.raise_for_status()
                total = int(resp.headers.get("Content-Length", 0))
                downloaded = 0
                with open(dest, "wb") as f:
                    async for chunk in resp.content.iter_chunked(1024 * 256):  # 256KB
                        f.write(chunk)
                        downloaded += len(chunk)
                        if progress_callback:
                            if asyncio.iscoroutinefunction(progress_callback):
                                await progress_callback(downloaded, total, *progress_args)
                            else:
                                progress_callback(downloaded, total, *progress_args)

    def _ffmpeg_merge(self, video: Path, audio: Path, output: Path):
        """Merge video-only + audio-only streams into a single mp4."""
        cmd = [
            "ffmpeg", "-y",
            "-i", str(video),
            "-i", str(audio),
            "-c:v", "copy",
            "-c:a", "aac",
            "-movflags", "+faststart",
            str(output),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg merge failed:\n{result.stderr[-500:]}")

    async def _download_thumbnail(self, thumb_url: str, title: str) -> Optional[Path]:
        """Download video thumbnail for use as Telegram thumb."""
        if not thumb_url:
            return None
        try:
            dest = self.download_dir / self._safe_name(title, "jpg")
            async with aiohttp.ClientSession() as session:
                async with session.get(thumb_url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status == 200:
                        with open(dest, "wb") as f:
                            f.write(await resp.read())
                        return dest
        except Exception as e:
            logger.debug(f"Thumbnail download failed (non-fatal): {e}")
        return None

    def _safe_name(self, title: str, ext: str) -> str:
        """Generate a safe filename from the video title."""
        import re
        safe = re.sub(r'[^\w\s\-.]', '', title)[:60].strip()
        safe = re.sub(r'\s+', '_', safe)
        return f"{safe}.{ext}"

    def cleanup(self, video_info: VideoInfo):
        """Delete downloaded files after successful upload."""
        for path in [video_info.local_path, video_info.thumbnail_path]:
            if path and path.exists():
                try:
                    path.unlink()
                    logger.debug(f"Deleted: {path}")
                except Exception as e:
                    logger.warning(f"Could not delete {path}: {e}")
