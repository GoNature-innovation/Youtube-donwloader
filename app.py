"""Web front end for the YouTube downloader — YouTube-styled dark UI.

A small Flask server over yt.py: open it in a browser, paste a link, pick a
mode, get the video and its transcript. Meant to be hosted on a Linux box and
used from another machine's browser, so everything downloads to the server and
is then offered back over HTTP.

Run:
    python app.py                      # http://0.0.0.0:8000
    python app.py --port 9000
    python app.py --downloads /srv/media
"""

from __future__ import annotations

import argparse
import json
import re
import threading
import time
import uuid
from collections import OrderedDict
from pathlib import Path

from flask import Flask, Response, abort, jsonify, render_template, request, send_file

import yt

try:
    from yt_dlp.utils import DownloadCancelled
except ImportError:  # older yt-dlp
    class DownloadCancelled(Exception):
        pass


QUALITIES = ["2160", "1440", "1080", "720", "480", "360", "best"]
AUDIO_FORMATS = ["mp3", "m4a", "opus", "wav", "flac"]
BROWSERS = ["", "chrome", "edge", "firefox", "brave", "opera", "vivaldi", "safari"]

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
PAUSE_BETWEEN = 3          # seconds between videos in a batch
MAX_JOBS = 20              # completed jobs kept for their logs and file lists

# Two different failures need two different cures.
# Rate limiting is about *how often* we asked — the only fix is to wait.
RATE_LIMITED = ("429", "too many requests", "temporarily")
BACKOFF = (15, 45)

# A 403 means the media URL itself was rejected: YouTube handed back a URL for
# a player client it then refuses to serve. Waiting never fixes it — asking as
# a different client does, so rotate through clients that are known to work.
BLOCKED = (
    "403", "forbidden", "unable to download video data",
    "page needs to be reloaded", "requested format is not available",
)
CLIENT_ROTATION = (None, ["mweb"], ["web_safari", "mweb", "default"])

BLOCKED_HINT = (
    "  tip: if every client fails, upload a cookies.txt (or name a browser "
    "installed on this server) to download as a signed-in user."
)
THROTTLE_HINT = (
    "  tip: YouTube is throttling this server. Wait a few minutes, or upload a "
    "cookies.txt to download as a signed-in user."
)


def clean_error(exc: Exception) -> str:
    """yt-dlp errors arrive colored and ERROR:-prefixed; strip both."""
    text = ANSI_RE.sub("", str(exc)).strip()
    return text[6:].strip() if text.startswith("ERROR:") else text


def _matches(message: str, markers: tuple[str, ...]) -> bool:
    lowered = message.lower()
    return any(marker in lowered for marker in markers)


def is_rate_limited(message: str) -> bool:
    return _matches(message, RATE_LIMITED)


def is_blocked(message: str) -> bool:
    return _matches(message, BLOCKED)


# --------------------------------------------------------------------------- #
# Paths — everything the browser can reach lives under one root
# --------------------------------------------------------------------------- #

app = Flask(__name__)
app.config["DOWNLOAD_ROOT"] = (Path.cwd() / "downloads").resolve()
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024  # cookies.txt uploads only


def download_root() -> Path:
    root = Path(app.config["DOWNLOAD_ROOT"])
    root.mkdir(parents=True, exist_ok=True)
    return root


def resolve_under_root(relative: str) -> Path:
    """Map a browser-supplied relative path to a real one inside the root.

    The server is reachable over the network, so a path from the page is never
    trusted: it is joined to the root and rejected unless it stays there.
    """
    root = download_root()
    candidate = (root / relative.strip().lstrip("/\\")).resolve() if relative.strip() else root
    if candidate != root and root not in candidate.parents:
        abort(400, "path escapes the downloads directory")
    return candidate


def rel_to_root(path: Path) -> str:
    return path.relative_to(download_root()).as_posix()


# --------------------------------------------------------------------------- #
# Jobs
# --------------------------------------------------------------------------- #

class Job:
    """One batch of URLs, running on its own thread and streamed over SSE.

    Events are appended to a list rather than pushed to a queue so that a page
    reload — or a second browser watching the same job — replays the whole run
    instead of joining halfway through.
    """

    def __init__(self, urls: list[str], args: argparse.Namespace, outdir: Path,
                 cookie_file: Path | None = None) -> None:
        self.id = uuid.uuid4().hex[:12]
        self.urls = urls
        self.args = args
        self.outdir = outdir
        self.cookie_file = cookie_file
        self.created = time.time()

        self.events: list[dict] = []
        self.condition = threading.Condition()
        self.cancel_flag = threading.Event()
        self.finished = False
        self.failures = 0
        self.files: list[dict] = []
        self.last_transcript: Path | None = None

    # -- event plumbing --------------------------------------------------- #
    def emit(self, kind: str, payload) -> None:
        with self.condition:
            self.events.append({"kind": kind, "payload": payload})
            if kind == "done":
                self.finished = True
            self.condition.notify_all()

    def stream(self):
        """Yield SSE frames: the backlog first, then live events."""
        index = 0
        while True:
            event = None
            with self.condition:
                while index >= len(self.events):
                    if self.finished:
                        return
                    if not self.condition.wait(timeout=15):
                        break  # nothing new — fall through to a heartbeat
                if index < len(self.events):
                    event = self.events[index]
                    index += 1

            # Yielded outside the lock: a slow client must not stall the worker.
            if event is None:
                yield ": ping\n\n"
                continue
            yield f"event: {event['kind']}\ndata: {json.dumps(event['payload'])}\n\n"
            if event["kind"] == "done":
                return

    def snapshot(self) -> dict:
        with self.condition:
            return {
                "id": self.id,
                "finished": self.finished,
                "cancelled": self.cancel_flag.is_set(),
                "failures": self.failures,
                "files": list(self.files),
                "events": list(self.events),
                "outdir": rel_to_root(self.outdir),
            }

    # -- output tracking -------------------------------------------------- #
    def note_file(self, name: str, kind: str) -> None:
        path = self.outdir / name
        if not path.exists():
            return
        entry = {
            "name": name,
            "path": rel_to_root(path),
            "kind": kind,
            "size": path.stat().st_size,
        }
        if entry not in self.files:
            self.files.append(entry)
            self.emit("file", entry)
        if kind == "transcript":
            self.last_transcript = path

    def log(self, message: str) -> None:
        for raw in ANSI_RE.sub("", message).split("\n"):
            line = raw.rstrip()
            stripped = line.strip()
            if not stripped:
                continue
            self.emit("log", line)

            # The worker only reports names; turn the ones that are real files
            # into something the browser can fetch.
            if "-> " in stripped:
                name = stripped.split("-> ", 1)[1]
                if stripped.startswith("media"):
                    self.note_file(name, "media")
                elif stripped.startswith("transcript") and name.endswith(".transcript.txt"):
                    self.note_file(name, "transcript")

    # -- run -------------------------------------------------------------- #
    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True).start()

    def _wait(self, seconds: float, status: str) -> bool:
        """Sleep in the worker thread; False if the user cancelled meanwhile."""
        self.emit("status", status)
        return not self.cancel_flag.wait(seconds)

    def _hook(self, d: dict) -> None:
        if self.cancel_flag.is_set():
            raise DownloadCancelled("cancelled by user")
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            done = d.get("downloaded_bytes") or 0
            pct = (done / total * 100) if total else 0
            self.emit("progress", pct)
            speed = d.get("speed")
            rate = f" · {speed / 1_048_576:.1f} MB/s" if speed else ""
            self.emit("status", f"{pct:.0f}%{rate}")
        elif d["status"] == "finished":
            self.emit("progress", 100)
            self.emit("status", "Processing…")

    def _run(self) -> None:
        stopped = False
        try:
            for index, url in enumerate(self.urls):
                if self.cancel_flag.is_set():
                    break
                if index and not self._wait(PAUSE_BETWEEN, "Pausing between videos…"):
                    break

                self.log(f"=== {url}")
                for attempt in range(len(CLIENT_ROTATION)):
                    # A cancel during extraction never reaches the progress
                    # hook, so catch it before starting another attempt.
                    if self.cancel_flag.is_set():
                        self.log("cancelled")
                        stopped = True
                        break
                    self.args.player_client = CLIENT_ROTATION[attempt]
                    try:
                        self.failures += yt.process(
                            url, self.args, log=self.log, progress_hook=self._hook,
                            sleep=lambda s: self.cancel_flag.wait(s),
                        )
                        break
                    except DownloadCancelled:
                        self.log("cancelled")
                        stopped = True
                        break
                    except Exception as exc:  # noqa: BLE001 - surface it in the log
                        message = clean_error(exc)
                        last_try = attempt == len(CLIENT_ROTATION) - 1

                        if not last_try and is_rate_limited(message):
                            wait = BACKOFF[min(attempt, len(BACKOFF) - 1)]
                            self.log(f"  {message}")
                            self.log(f"  rate limited — retrying in {wait}s…")
                            if not self._wait(wait, f"Retrying in {wait}s…"):
                                stopped = True
                                break
                            continue

                        if not last_try and is_blocked(message):
                            following = CLIENT_ROTATION[attempt + 1]
                            self.log(f"  {message}")
                            self.log(f"  blocked — retrying as '{following[0]}' client…")
                            self.emit("status", "Trying another client…")
                            continue

                        self.log(f"error: {message}")
                        if is_rate_limited(message):
                            self.log(THROTTLE_HINT)
                        elif is_blocked(message):
                            self.log(BLOCKED_HINT)
                        self.failures += 1
                        break
                if stopped:
                    break
        except Exception as exc:  # noqa: BLE001 - a dead thread would hang the page
            self.log(f"error: {clean_error(exc)}")
            self.failures += 1
        finally:
            if self.cookie_file:
                self.cookie_file.unlink(missing_ok=True)
            self.emit("done", {
                "failures": self.failures,
                "cancelled": self.cancel_flag.is_set(),
            })


JOBS: "OrderedDict[str, Job]" = OrderedDict()
JOBS_LOCK = threading.Lock()


def remember(job: Job) -> None:
    with JOBS_LOCK:
        JOBS[job.id] = job
        while len(JOBS) > MAX_JOBS:
            _, old = JOBS.popitem(last=False)
            old.cancel_flag.set()


def get_job(job_id: str) -> Job:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if job is None:
        abort(404, "no such job")
    return job


# --------------------------------------------------------------------------- #
# Request -> yt.py arguments
# --------------------------------------------------------------------------- #

def truthy(value) -> bool:
    return str(value).lower() in {"1", "true", "yes", "on"}


def build_args(form, outdir: Path, cookie_file: Path | None) -> argparse.Namespace:
    mode = form.get("mode", "video")
    quality = form.get("quality", "1080")
    return argparse.Namespace(
        output=str(outdir),
        quality="" if quality in {"best", ""} else quality,
        audio_only=mode == "audio",
        audio_format=form.get("audio_format", "mp3"),
        transcript_only=mode == "transcript",
        no_transcript=mode != "transcript" and not truthy(form.get("transcript", "true")),
        lang=form.get("lang") or "en",
        timestamps=truthy(form.get("timestamps")),
        keep_vtt=truthy(form.get("keep_vtt")),
        playlist=truthy(form.get("playlist")),
        cookies=str(cookie_file) if cookie_file else None,
        cookies_from_browser=form.get("browser") or None,
        player_client=None,  # set per attempt by the client rotation
        quiet=True,
    )


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #

@app.get("/")
def index():
    return render_template(
        "index.html",
        qualities=QUALITIES,
        audio_formats=AUDIO_FORMATS,
        browsers=BROWSERS,
        root=str(download_root()),
    )


@app.post("/api/jobs")
def create_job():
    form = request.form if request.form else (request.get_json(silent=True) or {})
    urls = [u.strip() for u in (form.get("urls") or "").splitlines() if u.strip()]
    if not urls:
        return jsonify(error="Paste at least one YouTube link."), 400

    outdir = resolve_under_root(form.get("subfolder", ""))
    outdir.mkdir(parents=True, exist_ok=True)

    # A headless server has no browser to read cookies from, so an uploaded
    # cookies.txt is the practical way past age gates and bot checks.
    cookie_file = None
    upload = request.files.get("cookies") if request.files else None
    if upload and upload.filename:
        cookie_file = download_root() / f".cookies-{uuid.uuid4().hex}.txt"
        upload.save(cookie_file)

    job = Job(urls, build_args(form, outdir, cookie_file), outdir, cookie_file)
    remember(job)
    job.start()
    return jsonify(id=job.id, urls=len(urls), outdir=rel_to_root(outdir))


@app.get("/api/jobs/<job_id>")
def job_state(job_id: str):
    return jsonify(get_job(job_id).snapshot())


@app.get("/api/jobs/<job_id>/events")
def job_events(job_id: str):
    job = get_job(job_id)
    return Response(
        job.stream(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/jobs/<job_id>/cancel")
def cancel_job(job_id: str):
    job = get_job(job_id)
    job.cancel_flag.set()
    job.emit("status", "Cancelling…")
    return jsonify(ok=True)


@app.get("/api/jobs/<job_id>/transcript")
def job_transcript(job_id: str):
    """Plain text for the copy button — the fast path into an AI prompt."""
    job = get_job(job_id)
    path = job.last_transcript
    if not path or not path.exists():
        return jsonify(error="no transcript yet"), 404
    text = path.read_text(encoding="utf-8")
    return jsonify(name=path.name, words=len(text.split()), text=text)


@app.get("/api/files")
def list_files():
    """Everything already on the server, newest first."""
    root = download_root()
    files = [
        {
            "name": p.name,
            "path": rel_to_root(p),
            "size": p.stat().st_size,
            "modified": p.stat().st_mtime,
        }
        for p in root.rglob("*")
        if p.is_file() and not p.name.startswith(".cookies-")
    ]
    files.sort(key=lambda f: f["modified"], reverse=True)
    return jsonify(root=str(root), files=files[:200])


@app.get("/files/<path:relative>")
def get_file(relative: str):
    path = resolve_under_root(relative)
    if not path.is_file():
        abort(404)
    inline = truthy(request.args.get("inline"))
    return send_file(path, as_attachment=not inline, download_name=path.name)


def main() -> int:
    parser = argparse.ArgumentParser(description="Serve the YouTube downloader over HTTP.")
    parser.add_argument("--host", default="0.0.0.0", help="bind address (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8000, help="port (default: 8000)")
    parser.add_argument(
        "--downloads", default="downloads",
        help="directory files are written to and served from (default: ./downloads)",
    )
    parser.add_argument("--debug", action="store_true", help="Flask debug mode")
    args = parser.parse_args()

    app.config["DOWNLOAD_ROOT"] = Path(args.downloads).expanduser().resolve()
    download_root()
    print(f"downloads -> {app.config['DOWNLOAD_ROOT']}")
    print(f"serving   -> http://{args.host}:{args.port}")
    # threaded: SSE holds a connection open for the whole download.
    app.run(host=args.host, port=args.port, debug=args.debug, threaded=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
