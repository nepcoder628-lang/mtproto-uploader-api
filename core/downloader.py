"""
Video Downloader
================
Uses yt-dlp with js_runtimes (node) to extract direct download URLs
without requiring cookies or bot detection workarounds.

Extracts best muxed MP4 (video+audio) and best audio-only stream,
then downloads directly via aiohttp.

For higher qualities (720p/1080p) that are video-only, merges with
audio via ffmpeg.
"""

import asyncio
import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Union

import aiohttp

logger = logging.getLogger(__name__)


@dataclass
class VideoInfo:
    """Metadata about a video."""
    title: str
    url: str
    duration: int
    width: int
    height: int
    ext: str
    filesize: int
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


# yt-dlp format selectors per quality preset
QUALITY_FORMATS: Dict[str, str] = {
    "best":  "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best",
    "1080p": "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=1080]+bestaudio/best[height<=1080]",
    "720p":  "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=720]+bestaudio/best[height<=720]",
    "480p":  "bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=480]+bestaudio/best[height<=480]",
    "360p":  "bestvideo[height<=360][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=360]+bestaudio/best[height<=360]",
    "144p":  "bestvideo[height<=144][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=144]+bestaudio/best[height<=144]",
    "worst": "worstvideo+worstaudio/worst",
    "audio": "bestaudio[ext=m4a]/bestaudio",
}

YDL_OPTS_BASE = {
    "quiet": True,
    "skip_download": True,
    "noplaylist": True,
    "ignoreerrors": True,
    "js_runtimes": {
        "node": {}
    },
}


class YouTubeDownloader:
    """
    Download videos using yt-dlp (js_runtimes/node) for URL extraction,
    then streams the file directly via aiohttp.

    Quality presets: best, 1080p, 720p, 480p, 360p, 144p, worst, audio
    """

    def __init__(
        self,
        download_dir: Union[str, Path] = "/tmp/mtproto_downloads",
        max_filesize_mb: int = 2000,
        cookies_file: Optional[str] = None,  # kept for API compat, not used
    ):
        self.download_dir = Path(download_dir)
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.max_filesize_mb = max_filesize_mb

    # ── URL extraction via yt-dlp ───────────────────────────────────────────

    def _extract_info(self, url: str, fmt: Optional[str] = None) -> dict:
        """Run yt-dlp extraction synchronously (called via executor)."""
        import yt_dlp
        opts = dict(YDL_OPTS_BASE)
        if fmt:
            opts["format"] = fmt
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if not info:
                raise RuntimeError("yt-dlp returned no info for this URL")
            return info

    def _get_direct_links(self, url: str) -> dict:
        """
        Extract best muxed MP4 and best audio URL using the exact
        logic from the reference implementation.
        """
        import yt_dlp
        opts = dict(YDL_OPTS_BASE)
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if not info:
                raise RuntimeError("Failed to extract video info")

            formats = info.get("formats", [])
            best_mp4 = None
            best_audio = None

            for f in formats:
                # Best muxed MP4 (video + audio)
                if (
                    f.get("ext") == "mp4"
                    and f.get("vcodec") != "none"
                    and f.get("acodec") != "none"
                ):
                    best_mp4 = f

                # Best audio-only
                if f.get("vcodec") == "none" and f.get("acodec") != "none":
                    best_audio = f

            return {
                "info": info,
                "best_mp4": best_mp4,
                "best_audio": best_audio,
            }

    def _get_links_for_quality(self, url: str, quality: str) -> dict:
        """
        Extract video + audio URLs for a specific quality.
        For higher qualities (720p+) yt-dlp returns separate video/audio streams.
        """
        import yt_dlp
        fmt = QUALITY_FORMATS.get(quality, QUALITY_FORMATS["720p"])
        opts = dict(YDL_OPTS_BASE)
        opts["format"] = fmt
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if not info:
                raise RuntimeError("Failed to extract video info")

            formats = info.get("formats", [])
            # After format selection, requested_formats contains the chosen streams
            requested = info.get("requested_formats") or []

            video_url = None
            audio_url = None
            video_ext = "mp4"
            audio_ext = "m4a"
            video_height = info.get("height") or 0

            if requested:
                for f in requested:
                    if f.get("vcodec") != "none" and f.get("acodec") == "none":
                        video_url = f.get("url")
                        video_ext = f.get("ext", "mp4")
                        video_height = f.get("height") or video_height
                    elif f.get("vcodec") == "none" and f.get("acodec") != "none":
                        audio_url = f.get("url")
                        audio_ext = f.get("ext", "m4a")
                    elif f.get("vcodec") != "none" and f.get("acodec") != "none":
                        # Muxed — no merge needed
                        video_url = f.get("url")
                        video_ext = f.get("ext", "mp4")
                        video_height = f.get("height") or video_height
            else:
                # Fallback: single format selected
                video_url = info.get("url")
                video_ext = info.get("ext", "mp4")
                video_height = info.get("height") or 0

            return {
                "info": info,
                "video_url": video_url,
                "audio_url": audio_url,
                "video_ext": video_ext,
                "audio_ext": audio_ext,
                "video_height": video_height,
                "is_muxed": audio_url is None,
            }

    # ── Public interface ────────────────────────────────────────────────────

    async def get_info(self, url: str) -> VideoInfo:
        """Fetch video metadata without downloading."""
        loop = asyncio.get_event_loop()
        info = await loop.run_in_executor(None, self._extract_info, url)
        return self._parse_info(info, url)

    def _parse_info(self, info: dict, url: str) -> VideoInfo:
        if "entries" in info:
            info = next(iter(info["entries"]))
        formats = info.get("formats", [])
        best = None
        for f in reversed(formats):
            if f.get("vcodec") != "none":
                best = f
                break
        return VideoInfo(
            title=info.get("title", "Unknown"),
            url=url,
            duration=int(info.get("duration") or 0),
            width=int(info.get("width") or (best.get("width") if best else 0) or 0),
            height=int(info.get("height") or (best.get("height") if best else 0) or 0),
            ext=info.get("ext", "mp4"),
            filesize=int(info.get("filesize") or info.get("filesize_approx") or 0),
            thumbnail_url=info.get("thumbnail", ""),
            uploader=info.get("uploader", ""),
            view_count=int(info.get("view_count") or 0),
            description=info.get("description", "")[:500],
        )

    async def get_available_qualities(self, url: str) -> List[Dict]:
        """List available quality options for a video."""
        loop = asyncio.get_event_loop()
        info = await loop.run_in_executor(None, self._extract_info, url)
        formats = info.get("formats", [])
        seen = set()
        result = []
        for f in reversed(formats):
            h = f.get("height")
            if not h:
                continue
            label = f"{h}p"
            if label in seen:
                continue
            seen.add(label)
            result.append({
                "quality": label,
                "width": f.get("width"),
                "height": h,
                "fps": f.get("fps"),
                "filesize": f.get("filesize") or f.get("filesize_approx"),
                "format_id": f.get("format_id"),
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
        Download a video.

        1. Uses yt-dlp (js_runtimes/node) to extract direct stream URLs
        2. Streams the file(s) via aiohttp
        3. If separate video+audio streams, merges with ffmpeg
        """
        loop = asyncio.get_event_loop()

        if quality == "audio":
            # Audio-only: use simple extraction
            data = await loop.run_in_executor(None, self._get_direct_links, url)
            info = self._parse_info(data["info"], url)
            audio_fmt = data.get("best_audio")
            if not audio_fmt:
                raise ValueError("No audio stream available.")
            out_path = self.download_dir / self._safe_name(info.title, "m4a")
            await self._download_file(audio_fmt["url"], out_path, progress_callback, progress_args)
            info.local_path = out_path
            info.filesize = out_path.stat().st_size
            info.thumbnail_path = await self._download_thumbnail(info.thumbnail_url, info.title)
            return info

        # For all other qualities use format-specific extraction
        data = await loop.run_in_executor(
            None, self._get_links_for_quality, url, quality
        )
        info = self._parse_info(data["info"], url)
        info.height = data["video_height"]

        video_url = data["video_url"]
        audio_url = data["audio_url"]
        is_muxed = data["is_muxed"]
        video_ext = data["video_ext"]
        audio_ext = data["audio_ext"]

        if not video_url:
            raise ValueError(f"No video stream found for quality '{quality}'.")

        if is_muxed:
            # Single muxed file — download directly
            out_path = self.download_dir / self._safe_name(info.title, video_ext)
            await self._download_file(video_url, out_path, progress_callback, progress_args)
        else:
            # Separate video + audio — download both, merge with ffmpeg
            video_tmp = self.download_dir / self._safe_name(info.title + "_v", video_ext)
            audio_tmp = self.download_dir / self._safe_name(info.title + "_a", audio_ext)
            out_path = self.download_dir / self._safe_name(info.title, "mp4")

            logger.info(f"Downloading video stream...")
            await self._download_file(video_url, video_tmp, progress_callback, progress_args)

            logger.info("Downloading audio stream...")
            await self._download_file(audio_url, audio_tmp, None, ())

            logger.info("Merging with ffmpeg...")
            await loop.run_in_executor(None, self._ffmpeg_merge, video_tmp, audio_tmp, out_path)

            for p in [video_tmp, audio_tmp]:
                try:
                    p.unlink()
                except Exception:
                    pass

        info.local_path = out_path
        info.filesize = out_path.stat().st_size if out_path.exists() else 0
        info.thumbnail_path = await self._download_thumbnail(info.thumbnail_url, info.title)

        logger.info(f"Download complete: {out_path.name} ({info.filesize_mb:.1f} MB)")
        return info

    # ── Internals ───────────────────────────────────────────────────────────

    async def _download_file(
        self,
        url: str,
        dest: Path,
        progress_callback: Optional[Callable],
        progress_args: tuple,
    ):
        """Stream-download a URL to a local file with optional progress."""
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=3600)) as resp:
                resp.raise_for_status()
                total = int(resp.headers.get("Content-Length", 0))
                downloaded = 0
                with open(dest, "wb") as f:
                    async for chunk in resp.content.iter_chunked(256 * 1024):
                        f.write(chunk)
                        downloaded += len(chunk)
                        if progress_callback:
                            if asyncio.iscoroutinefunction(progress_callback):
                                await progress_callback(downloaded, total, *progress_args)
                            else:
                                progress_callback(downloaded, total, *progress_args)

    def _ffmpeg_merge(self, video: Path, audio: Path, output: Path):
        """Merge separate video + audio streams into a single mp4."""
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
        """Download video thumbnail for Telegram thumb parameter."""
        if not thumb_url:
            return None
        try:
            dest = self.download_dir / self._safe_name(title + "_thumb", "jpg")
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
        """Generate a filesystem-safe filename from a title."""
        safe = re.sub(r'[^\w\s\-.]', '', title)[:60].strip()
        safe = re.sub(r'\s+', '_', safe)
        return f"{safe}.{ext}"

    def cleanup(self, video_info: VideoInfo):
        """Delete local files after successful upload."""
        for path in [video_info.local_path, video_info.thumbnail_path]:
            if path and path.exists():
                try:
                    path.unlink()
                    logger.debug(f"Deleted: {path}")
                except Exception as e:
                    logger.warning(f"Could not delete {path}: {e}")
