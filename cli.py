"""
MTProto Uploader — Standalone CLI
==================================
Upload a video from the command line.

Usage:
    python -m mtproto_uploader.cli upload --url "https://youtu.be/..." --chat-id 123456

Or set env vars and run directly.
"""

import argparse
import asyncio
import logging
import os
import sys

from mtproto_uploader import VideoUploadPipeline


def build_parser():
    parser = argparse.ArgumentParser(
        prog="mtproto-uploader",
        description="Upload YouTube videos to Telegram via MTProto",
    )
    sub = parser.add_subparsers(dest="command")

    # upload subcommand
    up = sub.add_parser("upload", help="Download and upload a video")
    up.add_argument("--url", required=True, help="Video URL")
    up.add_argument("--chat-id", required=True, help="Target Telegram chat ID")
    up.add_argument("--quality", default="720p",
                    choices=["best", "1080p", "720p", "480p", "360p", "worst", "audio"])
    up.add_argument("--caption", help="Message caption")
    up.add_argument("--document", action="store_true", help="Send as document")

    # info subcommand
    inf = sub.add_parser("info", help="Get video info without downloading")
    inf.add_argument("--url", required=True)

    # session subcommand
    sub.add_parser("session", help="Export current session string")

    return parser


async def run(args):
    api_id = int(os.environ["API_ID"])
    api_hash = os.environ["API_HASH"]
    session_string = os.environ.get("SESSION_STRING")
    phone_number = os.environ.get("PHONE_NUMBER")
    bot_token = os.environ.get("BOT_TOKEN")

    pipeline = VideoUploadPipeline(
        api_id=api_id,
        api_hash=api_hash,
        session_string=session_string,
        phone_number=phone_number,
        bot_token=bot_token,
    )

    async with pipeline:
        if args.command == "upload":
            print(f"Uploading: {args.url}")
            print(f"Quality: {args.quality}  →  Chat: {args.chat_id}")

            last_status = {"dl": 0, "ul": 0}

            def dl_progress(current, total):
                pct = current * 100 // max(total, 1)
                if pct != last_status["dl"]:
                    last_status["dl"] = pct
                    print(f"\rDownloading: {pct}%  ({current/1e6:.1f}/{total/1e6:.1f} MB)", end="")

            def ul_progress(current, total):
                pct = current * 100 // max(total, 1)
                if pct != last_status["ul"]:
                    last_status["ul"] = pct
                    print(f"\rUploading:   {pct}%  ({current/1e6:.1f}/{total/1e6:.1f} MB)", end="")

            result = await pipeline.process(
                url=args.url,
                chat_id=args.chat_id,
                quality=args.quality,
                caption=args.caption,
                send_as_document=args.document,
                download_progress_callback=dl_progress,
                upload_progress_callback=ul_progress,
            )
            print()
            print(
                f"\n✅ Done!  Message ID: {result.telegram_message_id}\n"
                f"   File: {result.video_info.title}\n"
                f"   Size: {result.video_info.filesize_mb:.1f} MB\n"
                f"   Time: {result.total_duration_seconds:.0f}s  "
                f"({result.average_speed_mbps:.1f} MB/s)"
            )

        elif args.command == "info":
            info = await pipeline.get_video_info(args.url)
            print(f"Title:      {info.title}")
            print(f"Duration:   {info.duration_human}")
            print(f"Resolution: {info.resolution}")
            print(f"Size:       ~{info.filesize_mb:.1f} MB")
            print(f"Uploader:   {info.uploader}")
            print(f"Views:      {info.view_count:,}")

        elif args.command == "session":
            session = await pipeline.export_session()
            print("SESSION_STRING:")
            print(session)
        else:
            build_parser().print_help()


def main():
    logging.basicConfig(level=logging.WARNING)
    parser = build_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    asyncio.run(run(args))


if __name__ == "__main__":
    main()
