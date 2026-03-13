"""
YouTube Downloader
==================
Uses yt-dlp to download YouTube videos with quality selection,
thumbnail extraction, and automatic metadata parsing.

Supports:
- YouTube, Vimeo, Twitter/X, Instagram, TikTok, and 1000+ sites
- Quality selection: best, worst, 1080p, 720p, 480p, 360p
- Format selection: mp4, mkv, webm
- Automatic thumbnail download for Telegram video thumb
- Progress reporting during download
"""

import asyncio
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Union

logger = logging.getLogger(__name__)


@dataclass
class VideoInfo:
    """Metadata about a downloaded video."""
    title: str
    url: str
    duration: int          # seconds
    width: int
    height: int
    ext: str               # file extension
    filesize: int          # bytes (0 if unknown before download)
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
    Download videos from YouTube and 1000+ other sites using yt-dlp.

    Quality presets:
        "best"    → best available quality (may be very large)
        "1080p"   → up to 1080p, merged to mp4
        "720p"    → up to 720p  (recommended for most users)
        "480p"    → up to 480p
        "360p"    → up to 360p  (smallest)
        "worst"   → worst quality (smallest file)
        "audio"   → audio only (mp3)

    Usage:
        dl = YouTubeDownloader(download_dir="/tmp/videos")
        info = await dl.download(
            url="https://youtu.be/dQw4w9WgXcQ",
            quality="720p",
            progress_callback=my_callback,
        )
        print(info.local_path, info.duration)
    """

    # yt-dlp format strings for each quality preset
    QUALITY_FORMATS: Dict[str, str] = {
        "best":   "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best",
        "1080p":  "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=1080]+bestaudio/best[height<=1080]",
        "720p":   "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=720]+bestaudio/best[height<=720]",
        "480p":   "bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=480]+bestaudio/best[height<=480]",
        "360p":   "bestvideo[height<=360][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=360]+bestaudio/best[height<=360]",
        "worst":  "worstvideo+worstaudio/worst",
        "audio":  "bestaudio[ext=m4a]/bestaudio",
    }

    def __init__(
        self,
        download_dir: Union[str, Path] = "/tmp/mtproto_downloads",
        max_filesize_mb: int = 2000,     # 2GB default limit
        cookies_file: Optional[str] = None,  # Path to cookies.txt for age-gated videos
    ):
        self.download_dir = Path(download_dir)
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.max_filesize_mb = max_filesize_mb
        self.cookies_file = cookies_file
        self._check_ytdlp()

    def _check_ytdlp(self):
        """Ensure yt-dlp Python package is installed."""
        try:
            import yt_dlp
            logger.debug(f"yt-dlp version: {yt_dlp.version.__version__}")
        except ImportError:
            raise RuntimeError(
                "yt-dlp not found. Install it with: pip install yt-dlp"
            )

    async def get_info(self, url: str) -> VideoInfo:
        """
        Fetch video metadata without downloading.

        Returns VideoInfo with title, duration, available qualities, etc.
        """
        import yt_dlp

        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": False,
        }
        if self.cookies_file:
            ydl_opts["cookiefile"] = self.cookies_file

        loop = asyncio.get_event_loop()

        def _extract():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                return ydl.extract_info(url, download=False)

        info_dict = await loop.run_in_executor(None, _extract)
        return self._parse_info(info_dict, url)

    def _parse_info(self, info: dict, url: str) -> VideoInfo:
        """Convert yt-dlp info dict to VideoInfo dataclass."""
        # Handle playlist — take first entry
        if "entries" in info:
            info = next(iter(info["entries"]))

        formats = info.get("formats", [])
        # Pick the best format to estimate dimensions/size
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

        Args:
            url: Video URL (YouTube, Vimeo, TikTok, Twitter, etc.)
            quality: One of: best, 1080p, 720p, 480p, 360p, worst, audio
            output_filename: Custom filename (without extension). Auto-generated if None.
            progress_callback: Async or sync callable(downloaded_bytes, total_bytes, *progress_args)
            progress_args: Extra args for progress_callback

        Returns:
            VideoInfo with local_path set to the downloaded file.
        """
        import yt_dlp

        if quality not in self.QUALITY_FORMATS:
            raise ValueError(
                f"Invalid quality '{quality}'. "
                f"Choose from: {', '.join(self.QUALITY_FORMATS.keys())}"
            )

        fmt = self.QUALITY_FORMATS[quality]

        # Determine output template
        if output_filename:
            outtmpl = str(self.download_dir / f"{output_filename}.%(ext)s")
        else:
            # Safe filename from video title
            outtmpl = str(self.download_dir / "%(title).80s-%(id)s.%(ext)s")

        # Thumbnail path
        thumb_path_holder: List[Optional[Path]] = [None]
        downloaded_path_holder: List[Optional[Path]] = [None]
        total_size_holder: List[int] = [0]

        def _progress_hook(d: dict):
            status = d.get("status")
            if status == "downloading":
                downloaded = d.get("downloaded_bytes", 0)
                total = d.get("total_bytes") or d.get("total_bytes_estimate", 0)
                total_size_holder[0] = total
                if progress_callback:
                    if asyncio.iscoroutinefunction(progress_callback):
                        asyncio.create_task(
                            progress_callback(downloaded, total, *progress_args)
                        )
                    else:
                        progress_callback(downloaded, total, *progress_args)
            elif status == "finished":
                path = d.get("filename") or d.get("info_dict", {}).get("filepath")
                if path:
                    downloaded_path_holder[0] = Path(path)

        ydl_opts = {
            "format": fmt,
            "outtmpl": outtmpl,
            "merge_output_format": "mp4",
            "writethumbnail": True,        # Download thumbnail for Telegram thumb
            "postprocessors": [
                {
                    "key": "FFmpegThumbnailsConvertor",
                    "format": "jpg",
                },
            ],
            "progress_hooks": [_progress_hook],
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,            # Don't download playlists
            "max_filesize": self.max_filesize_mb * 1024 * 1024,
        }

        if self.cookies_file:
            ydl_opts["cookiefile"] = self.cookies_file

        loop = asyncio.get_event_loop()

        def _download():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                return info

        logger.info(f"Starting download: {url} at quality={quality}")
        info_dict = await loop.run_in_executor(None, _download)

        # Parse result
        video_info = self._parse_info(info_dict, url)

        # Find the downloaded file on disk
        # yt-dlp may change the extension after merging
        actual_path = self._find_downloaded_file(outtmpl, info_dict)
        video_info.local_path = actual_path

        # Find thumbnail
        thumb_path = self._find_thumbnail(actual_path)
        video_info.thumbnail_path = thumb_path

        # Update file size from actual file
        if actual_path and actual_path.exists():
            video_info.filesize = actual_path.stat().st_size

        logger.info(
            f"Download complete: {actual_path.name if actual_path else '?'} "
            f"({video_info.filesize_mb:.1f} MB)"
        )
        return video_info

    def _find_downloaded_file(
        self, outtmpl: str, info_dict: dict
    ) -> Optional[Path]:
        """
        Locate the actual downloaded file after yt-dlp finishes.
        yt-dlp may have merged streams and changed the extension.
        """
        import yt_dlp

        # Handle playlist — take first
        if "entries" in info_dict:
            info_dict = next(iter(info_dict["entries"]), info_dict)

        # Try yt-dlp's own resolution
        try:
            with yt_dlp.YoutubeDL({"quiet": True}) as ydl:
                filepath = ydl.prepare_filename(info_dict)
                # After merge, extension becomes .mp4
                for ext in [".mp4", ".mkv", ".webm", ".m4v", ".avi"]:
                    candidate = Path(filepath).with_suffix(ext)
                    if candidate.exists():
                        return candidate
        except Exception:
            pass

        # Fallback: scan download directory for recent files
        if self.download_dir.exists():
            files = sorted(
                self.download_dir.glob("*"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            for f in files:
                if f.suffix.lower() in [".mp4", ".mkv", ".webm", ".m4v"]:
                    return f

        return None

    def _find_thumbnail(self, video_path: Optional[Path]) -> Optional[Path]:
        """Find the thumbnail file downloaded alongside the video."""
        if not video_path:
            return None
        # yt-dlp downloads thumbnail as <videoname>.jpg / .webp
        for ext in [".jpg", ".jpeg", ".webp", ".png"]:
            thumb = video_path.with_suffix(ext)
            if thumb.exists():
                return thumb
        return None

    async def get_available_qualities(self, url: str) -> List[Dict]:
        """
        List all available quality options for a video.
        Useful for letting users pick quality before downloading.
        """
        import yt_dlp

        ydl_opts = {"quiet": True, "no_warnings": True}
        if self.cookies_file:
            ydl_opts["cookiefile"] = self.cookies_file

        loop = asyncio.get_event_loop()

        def _list():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                formats = info.get("formats", [])
                result = []
                seen = set()
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
                        "vcodec": f.get("vcodec"),
                        "filesize": f.get("filesize") or f.get("filesize_approx"),
                        "format_id": f.get("format_id"),
                    })
                return sorted(result, key=lambda x: x["height"], reverse=True)

        return await loop.run_in_executor(None, _list)

    def cleanup(self, video_info: VideoInfo):
        """Delete downloaded files after successful upload."""
        for path in [video_info.local_path, video_info.thumbnail_path]:
            if path and path.exists():
                try:
                    path.unlink()
                    logger.debug(f"Deleted: {path}")
                except Exception as e:
                    logger.warning(f"Could not delete {path}: {e}")
