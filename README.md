# Youtube-downloader

Download YouTube videos, audio, and transcripts — from a browser, over the
network. The server does the downloading; the page streams its progress and
hands the finished files back over HTTP.

## Install

```bash
pip install -r requirements.txt
```

`ffmpeg` must be on PATH (merging video+audio, and audio conversion). Node,
Deno, or Bun must also be installed — YouTube requires solving a JS challenge,
and without a runtime every download fails with HTTP 403.

```bash
sudo apt install ffmpeg nodejs        # Debian/Ubuntu
```

## Run the web app

```bash
python app.py                          # http://<server-ip>:8000
python app.py --port 9000
python app.py --downloads /srv/media   # where files are written and served from
```

Then open `http://<server-ip>:8000` in Chrome from any machine on the network.

Flask's built-in server is fine for one person on a LAN. To expose it more
widely, put it behind gunicorn/nginx — and note that the app has no
authentication, so anyone who can reach the port can download to the server and
read everything under the downloads directory.

```bash
gunicorn --workers 1 --threads 8 --timeout 0 --bind 0.0.0.0:8000 app:app
```

One worker: jobs live in that process's memory, so a second worker would not
see them. Threads carry the concurrent downloads and the progress streams.

### Cookies on a headless server

Age-restricted videos and bot checks need a signed-in session. "Cookies from"
reads a browser profile *on the server*, which a headless box usually does not
have — export a `cookies.txt` from your own browser and upload it instead.

## Command line

`yt.py` works standalone, no server involved:

```bash
python yt.py "https://www.youtube.com/watch?v=..."
python yt.py URL --audio-only --audio-format m4a
python yt.py URL --transcript-only --lang en,hi
python yt.py URL -o downloads -q 720
```
