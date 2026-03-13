"""
Video Downloader
================
Uses the prexzy API (https://apis.prexzyvilla.site/download/aio?url=...)
to get direct stream URLs — no yt-dlp, no bot detection issues.

Strategy:
- prexzy returns a list of media entries (type, url, quality).
- quality string format: "mp4 (720p)", "m4a (133kb/s)", "opus (158kb/s)"
- itag=18 / "mp4 (360p)" with ratebypass=yes is muxed (video+audio).
- All other video entries are video-only → must merge with best audio via ffmpeg.
- Prefer mp4 over webm for video, m4a over opus for audio.
"""

import asyncio
import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Union

import aiohttp
import requests

logger = logging.getLogger(__name__)

PREXZY_API = "https://apis.prexzyvilla.site/download/aio"


# ── Data classes ────────────────────────────────────────────────────────────

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


# ── prexzy API helpers ──────────────────────────────────────────────────────

def _extract_video_id(url: str) -> Optional[str]:
    """Extract YouTube video ID from a URL."""
    patterns = [
        r"youtu\.be/([^?&/#]+)",
        r"youtube\.com/watch\?.*v=([^&/#]+)",
        r"youtube\.com/embed/([^?&/#]+)",
        r"youtube\.com/shorts/([^?&/#]+)",
    ]
    for p in patterns:
        m = re.search(p, url)
        if m:
            return m.group(1)
    return None


def _fetch_prexzy(url: str) -> dict:
    """Call prexzy API and return the parsed JSON response."""
    resp = requests.get(PREXZY_API, params={"url": url}, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("status"):
        raise RuntimeError(f"prexzy API error: {data}")
    return data


def _parse_height(quality_str: str) -> int:
    """'mp4 (720p)' → 720, 'm4a (133kb/s)' → 0"""
    m = re.search(r'\((\d+)p\)', quality_str)
    return int(m.group(1)) if m else 0


def _parse_bitrate(quality_str: str) -> int:
    """'m4a (133kb/s)' → 133, 'opus (158kb/s)' → 158"""
    m = re.search(r'\((\d+)kb/s\)', quality_str)
    return int(m.group(1)) if m else 0


def _is_muxed(video_url: str) -> bool:
    """itag=18 muxed streams have ratebypass=yes in their URL."""
    return "ratebypass=yes" in video_url


def _select_video(medias: list, quality_preset: str) -> Optional[dict]:
    """
    Pick the best video-type entry for the requested quality.
    Prefers mp4 over webm. Selects highest height <= target.
    """
    videos = [m for m in medias if m.get("type") == "video"]

    # Separate by ext: prefer mp4
    mp4s = [v for v in videos if v["quality"].startswith("mp4")]
    webms = [v for v in videos if v["quality"].startswith("webm")]
    ordered = mp4s if mp4s else webms

    if not ordered:
        return None

    # Attach parsed height
    for v in ordered:
        v["_height"] = _parse_height(v["quality"])

    if quality_preset == "best":
        return max(ordered, key=lambda x: x["_height"])

    if quality_preset == "audio":
        return None  # audio-only, no video needed

    if quality_preset == "worst":
        return min(ordered, key=lambda x: x["_height"])

    # Numeric quality: "720p" → 720
    target = int(quality_preset.replace("p", ""))
    # Pick highest mp4 that is <= target
    candidates = [v for v in ordered if v["_height"] <= target]
    if candidates:
        return max(candidates, key=lambda x: x["_height"])
    # If nothing fits below target, return lowest available
    return min(ordered, key=lambda x: x["_height"])


def _select_audio(medias: list) -> Optional[dict]:
    """
    Pick the best audio-type entry.
    Prefers m4a over opus, then highest bitrate.
    """
    audios = [m for m in medias if m.get("type") == "audio"]
    if not audios:
        return None

    m4as = [a for a in audios if a["quality"].startswith("m4a")]
    opuses = [a for a in audios if a["quality"].startswith("opus")]
    ordered = m4as if m4as else opuses

    for a in ordered:
        a["_bitrate"] = _parse_bitrate(a["quality"])

    return max(ordered, key=lambda x: x["_bitrate"])


# ── Main downloader class ───────────────────────────────────────────────────

class YouTubeDownloader:
    """
    Download videos via prexzy API (no yt-dlp, no bot detection).

    Quality presets: best, 1080p, 720p, 480p, 360p, 240p, 144p, audio
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

    # ── Public interface ────────────────────────────────────────────────────

    async def get_info(self, url: str) -> VideoInfo:
        """Fetch video metadata without downloading."""
        loop = asyncio.get_event_loop()
        data = await loop.run_in_executor(None, _fetch_prexzy, url)
        return self._parse_info(data, url)

    def _parse_info(self, data: dict, url: str) -> VideoInfo:
        medias = data.get("medias", [])
        title = data.get("title", "Unknown")

        # Find best video height
        best_height = 0
        for m in medias:
            if m.get("type") == "video":
                h = _parse_height(m.get("quality", ""))
                if h > best_height:
                    best_height = h

        # Build thumbnail URL from video ID
        video_id = _extract_video_id(url)
        thumbnail_url = (
            f"https://i.ytimg.com/vi/{video_id}/sddefault.jpg"
            if video_id else ""
        )

        return VideoInfo(
            title=title,
            url=url,
            duration=0,          # prexzy doesn't return duration
            width=0,             # prexzy doesn't return width
            height=best_height,
            ext="mp4",
            filesize=0,          # prexzy doesn't return filesize
            thumbnail_url=thumbnail_url,
            uploader="",
            view_count=0,
            description="",
        )

    async def get_available_qualities(self, url: str) -> List[Dict]:
        """List available quality options for a video."""
        loop = asyncio.get_event_loop()
        data = await loop.run_in_executor(None, _fetch_prexzy, url)
        medias = data.get("medias", [])

        seen = set()
        result = []
        for m in medias:
            if m.get("type") != "video":
                continue
            h = _parse_height(m.get("quality", ""))
            if not h or h in seen:
                continue
            seen.add(h)
            result.append({
                "quality": f"{h}p",
                "width": None,
                "height": h,
                "fps": None,
                "filesize": None,
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

        1. Calls prexzy API to get direct stream URLs
        2. Streams file(s) via aiohttp
        3. If separate video+audio, merges with ffmpeg
        """
        loop = asyncio.get_event_loop()

        # Fetch media list from prexzy
        data = await loop.run_in_executor(None, _fetch_prexzy, url)
        info = self._parse_info(data, url)
        medias = data.get("medias", [])

        if quality == "audio":
            audio_entry = _select_audio(medias)
            if not audio_entry:
                raise ValueError("No audio stream available from prexzy.")
            ext = "m4a" if audio_entry["quality"].startswith("m4a") else "opus"
            out_path = self.download_dir / self._safe_name(info.title, ext)
            await self._download_file(
                audio_entry["url"], out_path, progress_callback, progress_args
            )
            info.local_path = out_path
            info.filesize = out_path.stat().st_size
            info.thumbnail_path = await self._download_thumbnail(
                info.thumbnail_url, info.title
            )
            return info

        # Select video stream
        video_entry = _select_video(medias, quality)
        if not video_entry:
            raise ValueError(f"No video stream found for quality '{quality}'.")

        info.height = video_entry["_height"]

        video_url = video_entry["url"]
        video_ext = "mp4" if video_entry["quality"].startswith("mp4") else "webm"

        if _is_muxed(video_url):
            # Muxed stream (itag=18, 360p) — download directly, no merge needed
            logger.info(f"Muxed stream detected, downloading directly ({quality})")
            out_path = self.download_dir / self._safe_name(info.title, video_ext)
            await self._download_file(
                video_url, out_path, progress_callback, progress_args
            )
        else:
            # Video-only stream — need to download audio separately and merge
            audio_entry = _select_audio(medias)
            if not audio_entry:
                raise ValueError("No audio stream available for merging.")

            audio_ext = "m4a" if audio_entry["quality"].startswith("m4a") else "opus"
            video_tmp = self.download_dir / self._safe_name(info.title + "_v", video_ext)
            audio_tmp = self.download_dir / self._safe_name(info.title + "_a", audio_ext)
            out_path = self.download_dir / self._safe_name(info.title, "mp4")

            logger.info(f"Downloading video stream ({quality})...")
            await self._download_file(
                video_entry["url"], video_tmp, progress_callback, progress_args
            )

            logger.info("Downloading audio stream...")
            await self._download_file(audio_entry["url"], audio_tmp, None, ())

            logger.info("Merging video+audio with ffmpeg...")
            await loop.run_in_executor(
                None, self._ffmpeg_merge, video_tmp, audio_tmp, out_path
            )

            for p in [video_tmp, audio_tmp]:
                try:
                    p.unlink()
                except Exception:
                    pass

        info.local_path = out_path
        info.filesize = out_path.stat().st_size if out_path.exists() else 0
        info.thumbnail_path = await self._download_thumbnail(
            info.thumbnail_url, info.title
        )

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
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=3600)
            ) as resp:
                resp.raise_for_status()
                total = int(resp.headers.get("Content-Length", 0))
                downloaded = 0
                with open(dest, "wb") as f:
                    async for chunk in resp.content.iter_chunked(256 * 1024):
                        f.write(chunk)
                        downloaded += len(chunk)
                        if progress_callback:
                            if asyncio.iscoroutinefunction(progress_callback):
                                await progress_callback(
                                    downloaded, total, *progress_args
                                )
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

    async def _download_thumbnail(
        self, thumb_url: str, title: str
    ) -> Optional[Path]:
        """Download video thumbnail for Telegram thumb parameter."""
        if not thumb_url:
            return None
        try:
            dest = self.download_dir / self._safe_name(title + "_thumb", "jpg")
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    thumb_url, timeout=aiohttp.ClientTimeout(total=15)
                ) as resp:
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
