#!/usr/bin/env python3
"""
Download Mister Rogers' Neighborhood episodes from misterrogers.org.

Fetches all episode URLs from the video-episodes sitemap and downloads
the highest resolution video available using yt-dlp (Brightcove hosted).

Based in part on https://gitlab.com/-/snippets/2100082 by Mathew Duggan.

Usage:
    python3 download_mr_rogers.py [options]

Requirements:
    pip install yt-dlp
"""

import argparse
import json
import logging
import os
import re
import ssl
import subprocess
import sys
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

SITEMAP_URL = "https://www.misterrogers.org/video-episodes-sitemap.xml"
WATCH_URL = "https://www.misterrogers.org/watch/"

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"

# Naming pattern from the GitLab snippet
RENAME_PREFIX = "mister-rogers-neighborhood"


def setup_logging(log_file: str | None, verbose: bool) -> logging.Logger:
    logger = logging.getLogger("mr_rogers")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(logging.Formatter(LOG_FORMAT))
    logger.addHandler(console)

    if log_file:
        fh = logging.FileHandler(log_file)
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter(LOG_FORMAT))
        logger.addHandler(fh)

    return logger


def fetch_sitemap_urls(logger: logging.Logger) -> list[str]:
    """Fetch and parse the sitemap XML, returning all episode URLs."""
    logger.info("Fetching sitemap: %s", SITEMAP_URL)
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    req = urllib.request.Request(
        SITEMAP_URL,
        headers={"User-Agent": "Mozilla/5.0 (mr-rogers-downloader)"},
    )
    with urllib.request.urlopen(req, context=ctx) as resp:
        data = resp.read()

    root = ET.fromstring(data)
    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    urls = [loc.text for loc in root.findall(".//s:url/s:loc", ns) if loc.text]
    logger.info("Found %d episode URLs in sitemap", len(urls))
    return urls


def build_output_template(download_dir: str) -> str:
    """Build the yt-dlp output template string."""
    return os.path.join(download_dir, "%(title)s [%(id)s].%(ext)s")


def episode_already_downloaded(download_dir: str, url: str, logger: logging.Logger) -> bool:
    """Check if an episode URL has already been downloaded by looking at the archive file."""
    archive_path = os.path.join(download_dir, ".downloaded_archive.txt")
    if not os.path.exists(archive_path):
        return False

    # Extract the slug from the URL to check against archive
    # yt-dlp archive format is "extractor video_id"
    # We can't know the video ID without querying, so we also keep our own URL log
    url_archive = os.path.join(download_dir, ".downloaded_urls.txt")
    if os.path.exists(url_archive):
        with open(url_archive, "r") as f:
            downloaded = {line.strip() for line in f}
        if url in downloaded:
            return True
    return False


def mark_url_downloaded(download_dir: str, url: str):
    """Record that a URL has been successfully downloaded."""
    url_archive = os.path.join(download_dir, ".downloaded_urls.txt")
    with open(url_archive, "a") as f:
        f.write(url + "\n")


def download_episode(
    url: str,
    download_dir: str,
    logger: logging.Logger,
    dry_run: bool = False,
    retries: int = 3,
) -> bool:
    """Download a single episode at the highest resolution using yt-dlp."""
    output_template = build_output_template(download_dir)
    archive_path = os.path.join(download_dir, ".downloaded_archive.txt")

    cmd = [
        sys.executable, "-m", "yt_dlp",
        "--no-check-certificates",
        # Select best video+audio, merge into mp4
        "-f", "bestvideo+bestaudio/best",
        "--merge-output-format", "mp4",
        # Output naming
        "-o", output_template,
        # Download archive to skip already-downloaded videos (by video ID)
        "--download-archive", archive_path,
        # Retry on transient errors
        "--retries", str(retries),
        "--fragment-retries", str(retries),
        # Embed metadata
        "--add-metadata",
        # Continue partial downloads
        "--continue",
        # Don't overwite existing files
        "--no-overwrites",
        url,
    ]

    if dry_run:
        cmd.insert(cmd.index(url), "--simulate")

    logger.debug("Running: %s", " ".join(cmd))

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=1800,  # 30-minute timeout per episode
        )

        if result.returncode == 0:
            logger.info("Successfully downloaded: %s", url)
            if result.stdout:
                logger.debug("stdout: %s", result.stdout[-500:])
            return True
        else:
            stderr = result.stderr[-1000:] if result.stderr else "(no stderr)"
            # Check if it was just "already downloaded"
            if "has already been recorded in the archive" in (result.stderr or ""):
                logger.info("Already in archive, skipping: %s", url)
                return True
            logger.error("Failed to download %s (exit %d): %s", url, result.returncode, stderr)
            return False

    except subprocess.TimeoutExpired:
        logger.error("Timeout downloading: %s", url)
        return False
    except Exception as e:
        logger.error("Exception downloading %s: %s", url, e)
        return False


def rename_episodes(download_dir: str, logger: logging.Logger, dry_run: bool = False):
    """Rename downloaded episodes to a standardized format.

    Parses episode numbers from titles like 'Episode 1234 (1968)' and renames to:
        mister-rogers-neighborhood_s{season}.e{episode}.mp4

    Episode numbers encode season and episode: e.g., Episode 1234 -> s12.e34
    """
    episode_pattern = re.compile(r"Episode\s+(\d{1,5})")

    for entry in Path(download_dir).iterdir():
        if not entry.is_file() or entry.suffix != ".mp4":
            continue
        if entry.name.startswith(RENAME_PREFIX):
            continue  # Already renamed
        if entry.name.startswith("."):
            continue

        match = episode_pattern.search(entry.name)
        if not match:
            logger.debug("No episode number found in: %s", entry.name)
            continue

        ep_num = match.group(1)
        if len(ep_num) >= 4:
            season = ep_num[:-2].zfill(2)
            episode = ep_num[-2:]
        elif len(ep_num) >= 2:
            season = "01"
            episode = ep_num.zfill(2)
        else:
            season = "01"
            episode = ep_num.zfill(2)

        new_name = f"{RENAME_PREFIX}_s{season}.e{episode}.mp4"
        new_path = entry.parent / new_name

        if new_path.exists():
            logger.warning("Target already exists, skipping rename: %s -> %s", entry.name, new_name)
            continue

        logger.info("Renaming: %s -> %s", entry.name, new_name)
        if not dry_run:
            entry.rename(new_path)


def main():
    parser = argparse.ArgumentParser(
        description="Download Mister Rogers' Neighborhood episodes from misterrogers.org"
    )
    parser.add_argument(
        "-o", "--output-dir",
        default=os.path.join(os.getcwd(), "downloads"),
        help="Directory to save downloaded videos (default: ./downloads)",
    )
    parser.add_argument(
        "--log-file",
        default="mr_rogers.log",
        help="Log file path (default: mr_rogers.log)",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable verbose/debug logging",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate downloads without actually saving files",
    )
    parser.add_argument(
        "--no-rename",
        action="store_true",
        help="Skip the episode renaming step",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Limit number of episodes to download (0 = all)",
    )
    parser.add_argument(
        "--start-from",
        type=int,
        default=0,
        help="Start from this index in the episode list (0-based)",
    )
    parser.add_argument(
        "--include-watch-page",
        action="store_true",
        help="Also download rotating episodes from /watch/ page",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=3,
        help="Number of retries for failed downloads (default: 3)",
    )

    args = parser.parse_args()
    logger = setup_logging(args.log_file, args.verbose)

    # Verify yt-dlp is available
    try:
        result = subprocess.run(
            [sys.executable, "-m", "yt_dlp", "--version"],
            capture_output=True, text=True,
        )
        logger.info("Using yt-dlp version: %s", result.stdout.strip())
    except Exception:
        logger.error("yt-dlp is not installed. Install it with: pip install yt-dlp")
        sys.exit(1)

    # Create output directory
    os.makedirs(args.output_dir, exist_ok=True)
    logger.info("Download directory: %s", os.path.abspath(args.output_dir))

    # Fetch episode URLs from sitemap
    urls = fetch_sitemap_urls(logger)

    # Apply start-from and limit
    if args.start_from > 0:
        logger.info("Starting from index %d", args.start_from)
        urls = urls[args.start_from:]
    if args.limit > 0:
        logger.info("Limiting to %d episodes", args.limit)
        urls = urls[: args.limit]

    # Track results
    success_count = 0
    fail_count = 0
    skip_count = 0

    logger.info("Starting download of %d episodes...", len(urls))

    for i, url in enumerate(urls):
        logger.info("[%d/%d] Processing: %s", i + 1, len(urls), url)

        # Check our URL-level archive
        if episode_already_downloaded(args.output_dir, url, logger):
            logger.info("Already downloaded (URL archive), skipping: %s", url)
            skip_count += 1
            continue

        ok = download_episode(
            url=url,
            download_dir=args.output_dir,
            logger=logger,
            dry_run=args.dry_run,
            retries=args.retries,
        )

        if ok:
            success_count += 1
            mark_url_downloaded(args.output_dir, url)
        else:
            fail_count += 1

    # Optionally download from /watch/ page too
    if args.include_watch_page:
        logger.info("Downloading from /watch/ page: %s", WATCH_URL)
        ok = download_episode(
            url=WATCH_URL,
            download_dir=args.output_dir,
            logger=logger,
            dry_run=args.dry_run,
            retries=args.retries,
        )
        if ok:
            success_count += 1

    # Rename step
    if not args.no_rename:
        logger.info("Renaming downloaded episodes...")
        rename_episodes(args.output_dir, logger, dry_run=args.dry_run)

    logger.info(
        "Done! Downloaded: %d, Failed: %d, Skipped: %d",
        success_count, fail_count, skip_count,
    )

    if fail_count > 0:
        logger.warning(
            "Some downloads failed. Re-run the script to retry — "
            "already-downloaded episodes will be skipped automatically."
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
