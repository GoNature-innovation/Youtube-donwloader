"""Desktop app for the YouTube downloader — YouTube-styled dark UI.

A Tkinter front end over yt.py: paste a link, pick a mode, get the video and
its transcript. No dependencies beyond yt-dlp (Tkinter ships with Python).

Run:
    python app.py
"""

from __future__ import annotations

import argparse
import queue
import re
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, font as tkfont, messagebox, ttk

import yt

try:
    from yt_dlp.utils import DownloadCancelled
except ImportError:  # older yt-dlp
    class DownloadCancelled(Exception):
        pass


# --------------------------------------------------------------------------- #
# YouTube dark palette
# --------------------------------------------------------------------------- #

BG = "#0f0f0f"          # page background
CARD = "#1c1c1c"        # raised panels
INPUT = "#121212"       # input wells
HOVER = "#3f3f3f"       # hover fill on dark chips
CHIP = "#272727"        # resting chip / secondary button
BORDER = "#303030"      # hairlines
TEXT = "#f1f1f1"        # primary text
MUTED = "#aaaaaa"       # secondary text
DIM = "#717171"         # disabled text
RED = "#ff0000"         # YouTube red
RED_HOVER = "#cc0000"
BLUE = "#3ea6ff"        # focus / links
GREEN = "#2ba640"       # success
ERROR = "#ff4e45"

QUALITIES = ["2160", "1440", "1080", "720", "480", "360", "best"]
AUDIO_FORMATS = ["mp3", "m4a", "opus", "wav", "flac"]
BROWSERS = ["", "chrome", "edge", "firefox", "brave", "opera", "vivaldi", "safari"]
PLACEHOLDER = "Paste a YouTube link  (one per line for several)"

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
PAUSE_BETWEEN = 3          # seconds between videos in a batch

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
    "  tip: if every client fails, pick your browser under 'Cookies from' to "
    "download as a signed-in user."
)
THROTTLE_HINT = (
    "  tip: YouTube is throttling this machine. Wait a few minutes, or pick "
    "your browser under 'Cookies from' to download as a signed-in user."
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


def ui_font(size: int = 10, weight: str = "normal") -> tuple:
    """Roboto is YouTube's typeface; fall back to what Windows/macOS ship."""
    families = set(tkfont.families())
    for name in ("Roboto", "Segoe UI Variable Text", "Segoe UI", "Helvetica Neue", "Arial"):
        if name in families:
            return (name, size, weight)
    return ("TkDefaultFont", size, weight)


def round_rect(canvas: tk.Canvas, x1, y1, x2, y2, r, **kwargs) -> int:
    """Rounded rectangle via a smoothed polygon — Tk has no native one."""
    points = [
        x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
        x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
        x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
    ]
    return canvas.create_polygon(points, smooth=True, **kwargs)


# --------------------------------------------------------------------------- #
# Pill widgets — YouTube's buttons and filter chips are fully rounded
# --------------------------------------------------------------------------- #

class Pill(tk.Canvas):
    """Rounded button. `kind` is 'primary' (red), 'ghost' (grey) or 'chip'."""

    def __init__(self, master, text, command=None, kind="ghost", height=36,
                 min_width=0, bg=BG, bold=False, **kw):
        super().__init__(master, height=height, highlightthickness=0, bd=0, bg=bg, **kw)
        self.text = text
        self.command = command
        self.kind = kind
        self.height = height
        self.font = ui_font(10, "bold" if bold else "normal")
        self.selected = False
        self.enabled = True
        self._hover = False

        pad = 18 if kind != "chip" else 14
        width = max(min_width, tkfont.Font(font=self.font).measure(text) + pad * 2)
        self.configure(width=width)

        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Button-1>", self._on_click)
        self._draw()

    # -- appearance ------------------------------------------------------- #
    def _colors(self) -> tuple[str, str]:
        if not self.enabled:
            return CHIP, DIM
        if self.kind == "primary":
            return (RED_HOVER if self._hover else RED), "#ffffff"
        if self.kind == "chip" and self.selected:
            return ("#ffffff" if not self._hover else "#e5e5e5"), BG
        return (HOVER if self._hover else CHIP), TEXT

    def _draw(self) -> None:
        self.delete("all")
        fill, fg = self._colors()
        w, h = int(self["width"]), self.height
        round_rect(self, 1, 1, w - 1, h - 1, h / 2, fill=fill, outline=fill)
        self.create_text(w / 2, h / 2, text=self.text, fill=fg, font=self.font)

    # -- behaviour -------------------------------------------------------- #
    def _on_enter(self, _=None) -> None:
        self._hover = True
        self.configure(cursor="hand2" if self.enabled else "arrow")
        self._draw()

    def _on_leave(self, _=None) -> None:
        self._hover = False
        self._draw()

    def _on_click(self, _=None) -> None:
        if self.enabled and self.command:
            self.command()

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = enabled
        self._draw()

    def set_selected(self, selected: bool) -> None:
        self.selected = selected
        self._draw()

    def set_text(self, text: str) -> None:
        self.text = text
        self._draw()


class ChipGroup(ttk.Frame):
    """A row of mutually exclusive filter chips, like YouTube's home page."""

    def __init__(self, master, options: list[tuple[str, str]], variable: tk.StringVar,
                 on_change=None, bg=BG):
        super().__init__(master, style="Card.TFrame")
        self.variable = variable
        self.on_change = on_change
        self.chips: dict[str, Pill] = {}
        for label, value in options:
            chip = Pill(self, label, kind="chip", height=32, bg=bg,
                        command=lambda v=value: self.select(v))
            chip.pack(side="left", padx=(0, 8))
            self.chips[value] = chip
        self.select(variable.get(), notify=False)

    def select(self, value: str, notify: bool = True) -> None:
        self.variable.set(value)
        for val, chip in self.chips.items():
            chip.set_selected(val == value)
        if notify and self.on_change:
            self.on_change()


# --------------------------------------------------------------------------- #
# App
# --------------------------------------------------------------------------- #

class DownloaderApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Downloader — YouTube video & transcript")
        self.geometry("900x790")
        self.minsize(780, 700)
        self.configure(bg=BG)

        self.events: queue.Queue = queue.Queue()
        self.cancel_flag = threading.Event()
        self.worker: threading.Thread | None = None
        self.last_transcript: Path | None = None

        self._init_styles()
        self._build_ui()
        self.after(100, self._drain_events)

    # ---------------------------------------------------------- theming --- #
    def _init_styles(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")  # the only built-in theme that fully recolors

        style.configure("Bg.TFrame", background=BG)
        style.configure("Card.TFrame", background=CARD)
        style.configure("Bg.TLabel", background=BG, foreground=TEXT, font=ui_font(10))
        style.configure("Card.TLabel", background=CARD, foreground=TEXT, font=ui_font(10))
        style.configure("Muted.TLabel", background=CARD, foreground=MUTED, font=ui_font(9))
        style.configure("Status.TLabel", background=BG, foreground=MUTED, font=ui_font(9))
        style.configure("Section.TLabel", background=CARD, foreground=TEXT,
                        font=ui_font(11, "bold"))

        style.configure(
            "YT.TCombobox", fieldbackground=INPUT, background=CHIP, foreground=TEXT,
            arrowcolor=MUTED, bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER,
            selectbackground=INPUT, selectforeground=TEXT, padding=5,
        )
        style.map(
            "YT.TCombobox",
            fieldbackground=[("readonly", INPUT), ("disabled", CARD)],
            foreground=[("disabled", DIM)],
            bordercolor=[("focus", BLUE)],
            arrowcolor=[("disabled", DIM)],
        )
        self.option_add("*TCombobox*Listbox.background", CHIP)
        self.option_add("*TCombobox*Listbox.foreground", TEXT)
        self.option_add("*TCombobox*Listbox.selectBackground", HOVER)
        self.option_add("*TCombobox*Listbox.selectForeground", TEXT)
        self.option_add("*TCombobox*Listbox.font", ui_font(10))

        style.configure(
            "YT.Horizontal.TProgressbar", troughcolor=CHIP, background=RED,
            bordercolor=CHIP, lightcolor=RED, darkcolor=RED, thickness=6,
        )
        style.configure(
            "YT.Vertical.TScrollbar", background=BORDER, troughcolor=BG,
            bordercolor=BG, arrowcolor=DIM, lightcolor=BORDER, darkcolor=BORDER,
        )
        style.map("YT.Vertical.TScrollbar", background=[("active", HOVER)])

    def _checkbox(self, master, text, variable) -> tk.Checkbutton:
        return tk.Checkbutton(
            master, text=text, variable=variable, bg=CARD, fg=TEXT, selectcolor=INPUT,
            activebackground=CARD, activeforeground=TEXT, highlightthickness=0, bd=0,
            font=ui_font(10), anchor="w", cursor="hand2",
        )

    def _entry(self, master, textvariable, width) -> tk.Entry:
        return tk.Entry(
            master, textvariable=textvariable, width=width, bg=INPUT, fg=TEXT,
            insertbackground=TEXT, relief="flat", font=ui_font(10),
            highlightthickness=1, highlightbackground=BORDER, highlightcolor=BLUE,
        )

    # --------------------------------------------------------------- UI --- #
    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(5, weight=1)

        self._build_header()
        self._build_search()
        self._build_chips()
        self._build_options()
        self._build_actions()
        self._build_log()
        self._sync_mode()

    def _build_header(self) -> None:
        header = ttk.Frame(self, style="Bg.TFrame")
        header.grid(row=0, column=0, sticky="ew", padx=24, pady=(18, 10))
        header.columnconfigure(1, weight=1)

        # The YouTube play badge, drawn rather than shipped as an asset.
        logo = tk.Canvas(header, width=44, height=30, bg=BG, highlightthickness=0, bd=0)
        logo.grid(row=0, column=0, sticky="w")
        round_rect(logo, 1, 2, 43, 28, 8, fill=RED, outline=RED)
        logo.create_polygon(18, 9, 18, 21, 29, 15, fill="#ffffff")

        wordmark = tk.Label(header, text="Downloader", bg=BG, fg=TEXT,
                            font=ui_font(16, "bold"))
        wordmark.grid(row=0, column=1, sticky="w", padx=(10, 0))

        tk.Label(header, text="video · audio · transcripts", bg=BG, fg=DIM,
                 font=ui_font(9)).grid(row=0, column=2, sticky="e")

        tk.Frame(self, bg=BORDER, height=1).grid(row=0, column=0, sticky="sew", padx=0)

    def _build_search(self) -> None:
        row = ttk.Frame(self, style="Bg.TFrame")
        row.grid(row=1, column=0, sticky="ew", padx=24, pady=(14, 4))
        row.columnconfigure(0, weight=1)

        well = tk.Frame(row, bg=INPUT, highlightthickness=1,
                        highlightbackground=BORDER, highlightcolor=BLUE)
        well.grid(row=0, column=0, sticky="ew")
        well.columnconfigure(0, weight=1)

        self.url_text = tk.Text(
            well, height=3, wrap="none", undo=True, bg=INPUT, fg=TEXT,
            insertbackground=TEXT, relief="flat", font=ui_font(10),
            highlightthickness=0, padx=14, pady=10,
        )
        self.url_text.grid(row=0, column=0, sticky="ew")
        self.url_text.bind("<FocusIn>", self._clear_placeholder)
        self.url_text.bind("<FocusOut>", self._restore_placeholder)
        self._placeholder_on = False
        self._restore_placeholder()

        buttons = ttk.Frame(row, style="Bg.TFrame")
        buttons.grid(row=0, column=1, sticky="n", padx=(12, 0))
        Pill(buttons, "Paste", command=self._paste, kind="ghost", height=34,
             min_width=96).pack(pady=(0, 8))
        self.start_btn = Pill(buttons, "Download", command=self._start, kind="primary",
                              height=38, min_width=140, bold=True)
        self.start_btn.pack()

    def _build_chips(self) -> None:
        bar = ttk.Frame(self, style="Bg.TFrame")
        bar.grid(row=2, column=0, sticky="ew", padx=24, pady=(14, 6))

        self.mode = tk.StringVar(value="video")
        group = ChipGroup(
            bar,
            [("Video + transcript", "video"), ("Audio only", "audio"),
             ("Transcript only", "transcript")],
            self.mode, on_change=self._sync_mode, bg=BG,
        )
        group.configure(style="Bg.TFrame")
        group.pack(side="left")

    def _build_options(self) -> None:
        card = ttk.Frame(self, style="Card.TFrame")
        card.grid(row=3, column=0, sticky="ew", padx=24, pady=6)
        card.columnconfigure(1, weight=1)
        card.columnconfigure(3, weight=1)

        ttk.Label(card, text="Settings", style="Section.TLabel").grid(
            row=0, column=0, columnspan=4, sticky="w", padx=16, pady=(14, 8)
        )

        # --- destination --------------------------------------------------- #
        ttk.Label(card, text="Save to", style="Muted.TLabel").grid(
            row=1, column=0, sticky="w", padx=(16, 8), pady=6
        )
        self.outdir = tk.StringVar(value=str(Path.cwd() / "downloads"))
        self._entry(card, self.outdir, 40).grid(
            row=1, column=1, columnspan=2, sticky="ew", pady=6, ipady=4
        )
        folder_row = ttk.Frame(card, style="Card.TFrame")
        folder_row.grid(row=1, column=3, sticky="e", padx=16)
        Pill(folder_row, "Browse", command=self._browse, height=30, bg=CARD).pack(side="left")
        Pill(folder_row, "Open", command=self._open_folder, height=30,
             bg=CARD).pack(side="left", padx=(8, 0))

        # --- dropdowns ----------------------------------------------------- #
        ttk.Label(card, text="Max quality", style="Muted.TLabel").grid(
            row=2, column=0, sticky="w", padx=(16, 8), pady=6
        )
        self.quality = tk.StringVar(value="1080")
        self.quality_box = ttk.Combobox(
            card, textvariable=self.quality, values=QUALITIES, state="readonly",
            width=10, style="YT.TCombobox", font=ui_font(10),
        )
        self.quality_box.grid(row=2, column=1, sticky="w", pady=6)

        ttk.Label(card, text="Audio format", style="Muted.TLabel").grid(
            row=2, column=2, sticky="w", padx=(16, 8), pady=6
        )
        self.audio_format = tk.StringVar(value="mp3")
        self.audio_box = ttk.Combobox(
            card, textvariable=self.audio_format, values=AUDIO_FORMATS, state="disabled",
            width=10, style="YT.TCombobox", font=ui_font(10),
        )
        self.audio_box.grid(row=2, column=3, sticky="w", padx=(0, 16), pady=6)

        ttk.Label(card, text="Caption langs", style="Muted.TLabel").grid(
            row=3, column=0, sticky="w", padx=(16, 8), pady=6
        )
        self.lang = tk.StringVar(value="en")
        self._entry(card, self.lang, 12).grid(row=3, column=1, sticky="w", pady=6, ipady=4)

        ttk.Label(card, text="Cookies from", style="Muted.TLabel").grid(
            row=3, column=2, sticky="w", padx=(16, 8), pady=6
        )
        self.browser = tk.StringVar(value="")
        ttk.Combobox(
            card, textvariable=self.browser, values=BROWSERS, state="readonly",
            width=10, style="YT.TCombobox", font=ui_font(10),
        ).grid(row=3, column=3, sticky="w", padx=(0, 16), pady=6)

        # --- toggles ------------------------------------------------------- #
        toggles = ttk.Frame(card, style="Card.TFrame")
        toggles.grid(row=4, column=0, columnspan=4, sticky="w", padx=13, pady=(6, 14))
        self.want_transcript = tk.BooleanVar(value=True)
        self.timestamps = tk.BooleanVar(value=False)
        self.keep_vtt = tk.BooleanVar(value=False)
        self.playlist = tk.BooleanVar(value=False)
        self.transcript_check = self._checkbox(toggles, "Get transcript", self.want_transcript)
        self.transcript_check.pack(side="left", padx=(0, 14))
        self._checkbox(toggles, "Timestamps", self.timestamps).pack(side="left", padx=(0, 14))
        self._checkbox(toggles, "Keep raw .vtt", self.keep_vtt).pack(side="left", padx=(0, 14))
        self._checkbox(toggles, "Whole playlist", self.playlist).pack(side="left")

    def _build_actions(self) -> None:
        row = ttk.Frame(self, style="Bg.TFrame")
        row.grid(row=4, column=0, sticky="ew", padx=24, pady=(10, 6))
        row.columnconfigure(0, weight=1)

        self.progress = ttk.Progressbar(
            row, mode="determinate", maximum=100, style="YT.Horizontal.TProgressbar"
        )
        self.progress.grid(row=0, column=0, sticky="ew", pady=(6, 0))

        self.status = tk.StringVar(value="Ready")
        ttk.Label(row, textvariable=self.status, style="Status.TLabel", width=24,
                  anchor="e").grid(row=0, column=1, padx=(14, 8))

        self.copy_btn = Pill(row, "Copy transcript", command=self._copy_transcript, height=32)
        self.copy_btn.grid(row=0, column=2)
        self.copy_btn.set_enabled(False)

        self.cancel_btn = Pill(row, "Cancel", command=self._cancel, height=32)
        self.cancel_btn.grid(row=0, column=3, padx=(8, 0))
        self.cancel_btn.set_enabled(False)

    def _build_log(self) -> None:
        card = ttk.Frame(self, style="Card.TFrame")
        card.grid(row=5, column=0, sticky="nsew", padx=24, pady=(6, 20))
        card.rowconfigure(1, weight=1)
        card.columnconfigure(0, weight=1)

        ttk.Label(card, text="Activity", style="Section.TLabel").grid(
            row=0, column=0, sticky="w", padx=16, pady=(12, 6)
        )

        self.log_text = tk.Text(
            card, wrap="word", state="disabled", height=10, bg=INPUT, fg=MUTED,
            relief="flat", font=("Consolas", 9), highlightthickness=0, padx=12, pady=8,
        )
        self.log_text.grid(row=1, column=0, sticky="nsew", padx=(16, 0), pady=(0, 16))
        scroll = ttk.Scrollbar(card, command=self.log_text.yview, style="YT.Vertical.TScrollbar")
        scroll.grid(row=1, column=1, sticky="ns", padx=(4, 16), pady=(0, 16))
        self.log_text["yscrollcommand"] = scroll.set

        self.log_text.tag_configure("title", foreground=TEXT, font=("Consolas", 9, "bold"))
        self.log_text.tag_configure("ok", foreground=GREEN)
        self.log_text.tag_configure("err", foreground=ERROR)
        self.log_text.tag_configure("muted", foreground=DIM)

    # ---------------------------------------------------------- helpers --- #
    def _sync_mode(self) -> None:
        mode = self.mode.get()
        self.quality_box["state"] = "readonly" if mode == "video" else "disabled"
        self.audio_box["state"] = "readonly" if mode == "audio" else "disabled"
        if mode == "transcript":
            self.want_transcript.set(True)
            self.transcript_check.configure(state="disabled", fg=DIM)
        else:
            self.transcript_check.configure(state="normal", fg=TEXT)

    def _clear_placeholder(self, _=None) -> None:
        if self._placeholder_on:
            self.url_text.delete("1.0", "end")
            self.url_text.configure(fg=TEXT)
            self._placeholder_on = False

    def _restore_placeholder(self, _=None) -> None:
        if not self.url_text.get("1.0", "end").strip():
            self.url_text.delete("1.0", "end")
            self.url_text.insert("1.0", PLACEHOLDER)
            self.url_text.configure(fg=DIM)
            self._placeholder_on = True

    def _urls(self) -> list[str]:
        if self._placeholder_on:
            return []
        return [u.strip() for u in self.url_text.get("1.0", "end").splitlines() if u.strip()]

    def _paste(self) -> None:
        try:
            text = self.clipboard_get().strip()
        except tk.TclError:
            return
        self._clear_placeholder()
        self.url_text.insert("insert", text + "\n")
        self.url_text.focus_set()

    def _browse(self) -> None:
        chosen = filedialog.askdirectory(initialdir=self.outdir.get() or str(Path.cwd()))
        if chosen:
            self.outdir.set(chosen)

    def _open_folder(self) -> None:
        path = Path(self.outdir.get()).expanduser()
        path.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            subprocess.Popen(["explorer", str(path)])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])

    def _copy_transcript(self) -> None:
        """Straight to the clipboard — the fast path into an AI prompt."""
        if not self.last_transcript or not self.last_transcript.exists():
            return
        text = self.last_transcript.read_text(encoding="utf-8")
        self.clipboard_clear()
        self.clipboard_append(text)
        words = len(text.split())
        self.status.set(f"Copied · {words:,} words")

    def _log(self, message: str) -> None:
        for line in ANSI_RE.sub("", message).split("\n"):
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("==="):
                tag = "muted"
            elif stripped.startswith("error") or "cancelled" in stripped:
                tag = "err"
            elif stripped.startswith(("media", "transcript")):
                tag = "ok" if "->" in stripped and "none" not in stripped else "muted"
            else:
                tag = "title"
            self.log_text["state"] = "normal"
            self.log_text.insert("end", line.rstrip() + "\n", tag)
            self.log_text.see("end")
            self.log_text["state"] = "disabled"

            # Remember the newest plain-text transcript for the copy button.
            if "transcript ->" in stripped and stripped.endswith(".transcript.txt"):
                name = stripped.split("-> ", 1)[1]
                path = Path(self.outdir.get()).expanduser() / name
                if path.exists():
                    self.last_transcript = path
                    self.copy_btn.set_enabled(True)

    # ------------------------------------------------------- run/cancel --- #
    def _build_args(self) -> argparse.Namespace:
        mode = self.mode.get()
        quality = self.quality.get()
        return argparse.Namespace(
            output=self.outdir.get(),
            quality="" if quality in {"best", ""} else quality,
            audio_only=mode == "audio",
            audio_format=self.audio_format.get(),
            transcript_only=mode == "transcript",
            no_transcript=not self.want_transcript.get(),
            lang=self.lang.get() or "en",
            timestamps=self.timestamps.get(),
            keep_vtt=self.keep_vtt.get(),
            playlist=self.playlist.get(),
            cookies=None,
            cookies_from_browser=self.browser.get() or None,
            player_client=None,  # set per attempt by the client rotation
            quiet=True,
        )

    def _start(self) -> None:
        urls = self._urls()
        if not urls:
            messagebox.showwarning("No link", "Paste at least one YouTube link.")
            return

        args = self._build_args()
        self.cancel_flag.clear()
        self.start_btn.set_enabled(False)
        self.cancel_btn.set_enabled(True)
        self.progress["value"] = 0
        self.status.set("Starting…")

        # Fresh log per batch, so the finished count matches what is on screen.
        self.log_text["state"] = "normal"
        self.log_text.delete("1.0", "end")
        self.log_text["state"] = "disabled"

        self.worker = threading.Thread(target=self._run, args=(urls, args), daemon=True)
        self.worker.start()

    def _cancel(self) -> None:
        self.cancel_flag.set()
        self.status.set("Cancelling…")

    def _run(self, urls: list[str], args: argparse.Namespace) -> None:
        """Worker thread: never touches widgets, only posts to self.events."""
        post = self.events.put

        def hook(d: dict) -> None:
            if self.cancel_flag.is_set():
                raise DownloadCancelled("cancelled by user")
            if d["status"] == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate")
                done = d.get("downloaded_bytes") or 0
                pct = (done / total * 100) if total else 0
                post(("progress", pct))
                speed = d.get("speed")
                rate = f" · {speed / 1_048_576:.1f} MB/s" if speed else ""
                post(("status", f"{pct:.0f}%{rate}"))
            elif d["status"] == "finished":
                post(("progress", 100))
                post(("status", "Processing…"))

        failures = 0
        stopped = False

        for index, url in enumerate(urls):
            if self.cancel_flag.is_set():
                break
            if index and not self._wait(PAUSE_BETWEEN, "Pausing between videos…"):
                break

            post(("log", f"=== {url}"))
            for attempt in range(len(CLIENT_ROTATION)):
                args.player_client = CLIENT_ROTATION[attempt]
                try:
                    failures += yt.process(
                        url, args, log=lambda m: post(("log", m)), progress_hook=hook,
                        sleep=lambda s: self.cancel_flag.wait(s),
                    )
                    break
                except DownloadCancelled:
                    post(("log", "cancelled"))
                    stopped = True
                    break
                except Exception as exc:  # noqa: BLE001 - surface it in the log
                    message = clean_error(exc)
                    last_try = attempt == len(CLIENT_ROTATION) - 1

                    if not last_try and is_rate_limited(message):
                        wait = BACKOFF[min(attempt, len(BACKOFF) - 1)]
                        post(("log", f"  {message}"))
                        post(("log", f"  rate limited — retrying in {wait}s…"))
                        if not self._wait(wait, f"Retrying in {wait}s…"):
                            stopped = True
                            break
                        continue

                    if not last_try and is_blocked(message):
                        following = CLIENT_ROTATION[attempt + 1]
                        post(("log", f"  {message}"))
                        post(("log", f"  blocked — retrying as '{following[0]}' client…"))
                        self.events.put(("status", "Trying another client…"))
                        continue

                    post(("log", f"error: {message}"))
                    if is_rate_limited(message):
                        post(("log", THROTTLE_HINT))
                    elif is_blocked(message):
                        post(("log", BLOCKED_HINT))
                    failures += 1
                    break
            if stopped:
                break

        post(("done", failures))

    def _wait(self, seconds: float, status: str) -> bool:
        """Sleep in the worker thread; False if the user cancelled meanwhile."""
        self.events.put(("status", status))
        return not self.cancel_flag.wait(seconds)

    def _drain_events(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "log":
                    self._log(payload)
                elif kind == "progress":
                    self.progress["value"] = payload
                elif kind == "status":
                    self.status.set(payload)
                elif kind == "done":
                    self.start_btn.set_enabled(True)
                    self.cancel_btn.set_enabled(False)
                    if self.cancel_flag.is_set():
                        self.status.set("Cancelled")
                    elif payload:
                        self.status.set(f"Finished · {payload} failed")
                    else:
                        self.status.set("Done")
                        self.progress["value"] = 100
        except queue.Empty:
            pass
        self.after(100, self._drain_events)


if __name__ == "__main__":
    DownloaderApp().mainloop()
