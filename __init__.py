"""
MTProto Uploader — Upload any file up to 2GB/4GB via Telegram MTProto.
Integrates with any Python Telegram bot framework.
"""
from .core.uploader import MTProtoUploader
from .core.downloader import YouTubeDownloader
from .core.pipeline import VideoUploadPipeline

__all__ = ["MTProtoUploader", "YouTubeDownloader", "VideoUploadPipeline"]
__version__ = "1.0.0"
