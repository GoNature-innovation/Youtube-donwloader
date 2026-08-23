"""YouTube downloader + transcript extractor.

Downloads a video (or just its audio) from a URL and writes out the transcript
as plain text and as a timestamped file.

Usage:
    python yt.py "https://www.youtube.com/watch?v=..."
    python yt.py URL --audio-only
    python yt.py URL --transcript-only --lang en,hi
    python yt.py URL -o downloads -q 720

Requires: yt-dlp (pip install -r requirements.txt) and ffmpeg on PATH for
merging video+audio and for --audio-only mp3 conversion.
"""

from __future__ import annotations

import argparse
import re
import sys
import textwrap
import time
from pathlib import Path

try:
    from yt_dlp import YoutubeDL
except ImportError:  # pragma: no cover
    sys.exit("yt-dlp is not installed. Run: pip install -r requirements.txt")


# --------------------------------------------------------------------------- #
# Transcript handling
# --------------------------------------------------------------------------- #

TIMING_RE = re.compile(
    r"^\d{2}:\d{2}:\d{2}[.,]\d{3}\s*-->\s*\d{2}:\d{2}:\d{2}[.,]\d{3}"
)
TAG_RE = re.compile(r"<[^>]+>")  # <00:00:01.000>, <c>, </c>, <i> ...


OVERLAP_WINDOW = 60  # words of context to check a new cue against


def merge_rolling(cues: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Strip the carry-over from YouTube's rolling auto-captions.

    Each cue repeats the tail of the one before it and appends a few new
    words, so "we feel very weak" arrives three times. Comparing whole cues
    only catches exact repeats; this trims the longest word-level overlap
    between what has been written and what the next cue starts with.
    """
    merged: list[tuple[str, str]] = []
    tail: list[str] = []

    for start, text in cues:
        words = text.split()
        if not words:
            continue

        overlap = 0
        for size in range(min(len(tail), len(words)), 0, -1):
            if [w.lower() for w in tail[-size:]] == [w.lower() for w in words[:size]]:
                overlap = size
                break

        fresh = words[overlap:]
        if not fresh:
            continue  # cue added nothing new
        merged.append((start, " ".join(fresh)))
        tail = (tail + fresh)[-OVERLAP_WINDOW:]

    return merged


def parse_vtt(vtt_text: str) -> list[tuple[str, str]]:
    """Turn a WebVTT/SRT caption file into [(start_timestamp, line), ...]."""
    cues: list[tuple[str, str]] = []
    start = ""
    buffer: list[str] = []

    def flush() -> None:
        if not buffer:
            return
        text = " ".join(buffer).strip()
        if text:
            cues.append((start, text))
        buffer.clear()

    for raw in vtt_text.splitlines():
        line = raw.strip()
        if TIMING_RE.match(line):
            flush()
            start = line.split("-->")[0].strip().replace(",", ".")[:8]
            continue
        if not line:
            flush()
            continue
        if line.startswith(("WEBVTT", "NOTE", "Kind:", "Language:", "STYLE")) or line.isdigit():
            continue  # headers and SRT cue numbers
        clean = TAG_RE.sub("", line).strip()
        if clean and clean not in buffer:
            buffer.append(clean)

    flush()
    return merge_rolling(cues)


def write_transcript(
    vtt_text: str, stem: str, outdir: Path, timestamps: bool = False
) -> Path | None:
    """Write the captions as one transcript file; None if there was no text."""
    cues = parse_vtt(vtt_text)
    if not cues:
        return None

    if timestamps:
        body = "\n".join(f"[{ts}] {text}" for ts, text in cues)
    else:
        # Wrapped rather than one endless line, so it reads in any editor.
        body = textwrap.fill(" ".join(text for _, text in cues), width=100)

    out = outdir / f"{stem}.transcript.txt"
    out.write_text(body + "\n", encoding="utf-8")
    return out


# --------------------------------------------------------------------------- #
# yt-dlp options
# --------------------------------------------------------------------------- #


def build_options(args: argparse.Namespace, outdir: Path) -> dict:
    opts: dict = {
        "outtmpl": str(outdir / "%(title).150B [%(id)s].%(ext)s"),
        "restrictfilenames": True,
        "noplaylist": not args.playlist,
        "ignoreerrors": args.playlist,
        "concurrent_fragment_downloads": 4,
        "quiet": args.quiet,
        "no_warnings": args.quiet,
        "no_color": True,  # keep ANSI codes out of logs and error messages
        # YouTube requires solving a JS challenge to get working media URLs.
        # yt-dlp only enables deno by default; node is far more commonly
        # installed, and without any runtime downloads fail with HTTP 403.
        "js_runtimes": {"deno": {}, "node": {}, "bun": {}},
        # YouTube throttles bursts hard (HTTP 429), so pace the requests and
        # retry generously rather than failing the whole video.
        "retries": 10,
        "fragment_retries": 10,
        "extractor_retries": 3,
        "sleep_interval_requests": 1,
    }

    if args.cookies_from_browser:
        opts["cookiesfrombrowser"] = (args.cookies_from_browser,)
    if args.cookies:
        opts["cookiefile"] = args.cookies

    # Which YouTube "player client" to impersonate. The defaults sometimes hand
    # back URLs that answer 403; naming a client is the way out of that.
    clients = getattr(args, "player_client", None)
    if clients:
        opts["extractor_args"] = {"youtube": {"player_client": list(clients)}}

    # Captions are deliberately NOT requested here — they are fetched in a
    # separate pass (see fetch_captions) because the clients that get media
    # past a 403 do not serve captions at all.

    # --- media ------------------------------------------------------------- #
    if args.transcript_only:
        opts["skip_download"] = True
    elif args.audio_only:
        opts["format"] = "bestaudio/best"
        opts["postprocessors"] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": args.audio_format,
                "preferredquality": "192",
            }
        ]
    else:
        height = f"[height<={args.quality}]" if args.quality else ""
        opts["format"] = (
            f"bestvideo{height}[ext=mp4]+bestaudio[ext=m4a]/"
            f"bestvideo{height}+bestaudio/best{height}/best"
        )
        opts["merge_output_format"] = "mp4"

    return opts


# --------------------------------------------------------------------------- #
# Captions — fetched separately from the media
# --------------------------------------------------------------------------- #


CAPTION_BACKOFF = (10, 30)  # waits before caption retry 1 and 2


def caption_options(args, outdir: Path) -> dict:
    """Options for the captions pass, always as YouTube's default client.

    Fallback clients such as mweb can rescue a 403 on the media, but they
    report no captions whatsoever, so this pass must never inherit them.
    """
    opts = {
        "outtmpl": str(outdir / "%(title).150B [%(id)s].%(ext)s"),
        "restrictfilenames": True,
        "noplaylist": True,
        "skip_download": True,
        "quiet": True,
        "no_warnings": True,
        "no_color": True,
        "noprogress": True,
        "js_runtimes": {"deno": {}, "node": {}, "bun": {}},
        "retries": 10,
        "extractor_retries": 3,
        "sleep_interval_requests": 1,
    }
    if args.cookies_from_browser:
        opts["cookiesfrombrowser"] = (args.cookies_from_browser,)
    if args.cookies:
        opts["cookiefile"] = args.cookies
    return opts


def pick_language(info: dict, tracks: dict, requested: list[str]) -> str | None:
    """Choose a caption language: what was asked for, else something sensible."""
    available = [lang for lang in tracks if lang != "live_chat"]
    if not available:
        return None

    for want in requested:                      # exact match, e.g. "en"
        if want in available:
            return want
    for want in requested:                      # regional variant, "en" -> "en-US"
        for lang in available:
            if lang.split("-")[0] == want.split("-")[0]:
                return lang

    # Nothing requested exists. The video's own language beats an arbitrary
    # auto-translation — YouTube lists a hundred machine-translated tracks, and
    # picking blind gets you Abkhazian for a Hindi video.
    spoken = info.get("language")
    if spoken and spoken in available:
        return spoken
    return available[0]


def fetch_captions(url: str, args, outdir: Path, log, sleep=time.sleep):
    """Fetch one video's captions. Returns (stem, language, vtt_text) or None.

    The caption track is pulled straight from its URL rather than through
    yt-dlp's subtitle writer, because that writer gives up on the first HTTP
    429 and YouTube rate-limits caption endpoints aggressively.
    """
    langs = [lang.strip() for lang in args.lang.split(",") if lang.strip()]

    try:
        with YoutubeDL(caption_options(args, outdir)) as ydl:
            info = ydl.extract_info(url, download=False)
            if not info:
                return None

            tracks = {
                **(info.get("subtitles") or {}),
                **(info.get("automatic_captions") or {}),
            }
            language = pick_language(info, tracks, langs)
            if not language:
                return None
            if language not in langs:
                log(f"  transcript -> no {'/'.join(langs)} captions, using '{language}'")

            options = tracks[language]
            track = next((t for t in options if t.get("ext") == "vtt"), options[0])
            stem = Path(ydl.prepare_filename(info)).stem

            for attempt in range(len(CAPTION_BACKOFF) + 1):
                try:
                    data = ydl.urlopen(track["url"]).read()
                    return stem, language, data.decode("utf-8", "replace")
                except Exception as exc:  # noqa: BLE001 - captions are best-effort
                    if attempt < len(CAPTION_BACKOFF) and "429" in str(exc):
                        wait = CAPTION_BACKOFF[attempt]
                        log(f"  transcript -> captions rate limited, retrying in {wait}s…")
                        sleep(wait)
                        continue
                    log(f"  transcript -> caption fetch failed: {exc}")
                    return None
    except Exception as exc:  # noqa: BLE001 - never let captions sink the video
        log(f"  transcript -> caption lookup failed: {exc}")
    return None


# --------------------------------------------------------------------------- #
# Main flow
# --------------------------------------------------------------------------- #


def process(
    url: str, args: argparse.Namespace, log=print, progress_hook=None, sleep=time.sleep
) -> int:
    """Download `url` and write its transcript. `log` receives status lines.

    Pass `progress_hook` (a yt-dlp progress callback) to drive a UI; doing so
    also silences yt-dlp's own console output. `sleep` covers caption retry
    waits, so a UI can supply a cancellable one.
    """
    outdir = Path(args.output).expanduser().resolve()
    outdir.mkdir(parents=True, exist_ok=True)

    opts = build_options(args, outdir)
    if progress_hook is not None:
        opts.update(progress_hooks=[progress_hook], quiet=True, no_warnings=True, noprogress=True)

    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)

    entries = info.get("entries") if info and "entries" in info else [info]
    failures = 0

    for entry in entries or []:
        if not entry:
            failures += 1
            continue

        title = entry.get("title", "video")
        video_id = entry.get("id", "")
        log(f"\n{title}")

        # yt-dlp still reports a filepath when skip_download is on, so confirm
        # the file actually landed before claiming it.
        for item in entry.get("requested_downloads") or []:
            path = item.get("filepath")
            if path and Path(path).exists():
                log(f"  media      -> {Path(path).name}")

        if args.no_transcript:
            continue

        # Separate pass on the default client, which is the only one that
        # actually serves captions.
        target = entry.get("webpage_url") or url
        captions = fetch_captions(target, args, outdir, log, sleep)
        if not captions:
            log("  transcript -> none available for this video")
            continue

        stem, language, vtt_text = captions
        if args.keep_vtt:
            (outdir / f"{stem}.{language}.vtt").write_text(vtt_text, encoding="utf-8")

        written = write_transcript(vtt_text, stem, outdir, args.timestamps)
        if written:
            log(f"  transcript -> {written.name}")
        else:
            log("  transcript -> captions were empty")

    return failures


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Download a YouTube video and extract its transcript."
    )
    parser.add_argument("urls", nargs="+", help="one or more YouTube URLs")
    parser.add_argument(
        "-o", "--output", default="downloads", help="output directory (default: downloads)"
    )
    parser.add_argument(
        "-q", "--quality", default="1080",
        help="max video height, e.g. 480/720/1080/2160, or 'best' (default: 1080)",
    )
    parser.add_argument("--audio-only", action="store_true", help="extract audio only")
    parser.add_argument(
        "--audio-format", default="mp3", help="audio codec for --audio-only (default: mp3)"
    )
    parser.add_argument(
        "--transcript-only", action="store_true", help="skip the media, fetch captions only"
    )
    parser.add_argument("--no-transcript", action="store_true", help="skip captions")
    parser.add_argument(
        "--lang", default="en", help="caption languages, comma separated (default: en)"
    )
    parser.add_argument(
        "--timestamps", action="store_true",
        help="write the transcript as timestamped lines instead of flowing text",
    )
    parser.add_argument("--keep-vtt", action="store_true", help="keep the raw .vtt files")
    parser.add_argument(
        "--playlist", action="store_true", help="download the whole playlist, not just one video"
    )
    parser.add_argument(
        "--cookies", help="path to a cookies.txt file (for age-restricted/private videos)"
    )
    parser.add_argument(
        "--cookies-from-browser", help="load cookies from a browser, e.g. chrome, firefox, edge"
    )
    parser.add_argument(
        "--player-client", default=None,
        help="YouTube client(s) to impersonate, e.g. mweb or web_safari,mweb — try this on HTTP 403",
    )
    parser.add_argument("--quiet", action="store_true", help="less yt-dlp output")
    args = parser.parse_args()

    if args.quality.lower() in {"best", "max", "none"}:
        args.quality = ""
    if args.player_client:
        args.player_client = [c.strip() for c in args.player_client.split(",") if c.strip()]

    failures = 0
    for url in args.urls:
        try:
            failures += process(url, args)
        except Exception as exc:  # noqa: BLE001 - report and keep going
            print(f"error: {url}: {exc}", file=sys.stderr)
            failures += 1

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
