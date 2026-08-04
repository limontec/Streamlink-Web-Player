# Streamlink Web Player

A local web UI for [Streamlink](https://streamlink.github.io/): paste a stream
URL, watch it in the browser via HLS, no VLC needed. Runs multiple streams at
once in a resizable, drag-to-reorder grid.

## Requirements

- Python 3.8+
- [Flask](https://flask.palletsprojects.com/) (`pip install flask`)
- [Streamlink](https://streamlink.github.io/install.html), available on your `PATH`
- [FFmpeg](https://ffmpeg.org/download.html), installed locally

## Setup

1. Install the Python dependencies:

   ```
   pip install -r requirements.txt
   ```

   This installs Flask and Streamlink. Confirm the `streamlink` command works from a terminal afterward.

2. Install FFmpeg and open `app.py` to point `FFMPEG_PATH` at your `ffmpeg.exe`:

   ```python
   FFMPEG_PATH = r"C:\ffmpeg\bin\ffmpeg.exe"
   ```

3. (Optional) Drop custom Streamlink plugins into the `custom_plugin/` folder
   next to `app.py` (created automatically on first run).

## Running

```
python app.py
```

Then open **http://localhost:5000** in your browser.

## Using it

1. Paste a stream URL into the input field and press Enter (or click **Watch**).
2. The server launches `streamlink` piped into `ffmpeg`, which transcodes to
   HLS; the video starts playing automatically once the first segment is ready.
3. Each stream gets its own card:
   - Drag the header to reorder cards.
   - Drag the bottom-right corner to resize a card.
   - **Reload** restarts that stream's player.
   - **Close** stops the stream and frees the slot.
   - **Show Logs** reveals a per-stream event log (connection state, buffering, errors).
4. The status dot next to each stream's title shows its state: yellow
   (starting/buffering), green (playing), red (disconnected/error).
5. Open streams are remembered in the browser (`localStorage`) and restored
   automatically next time you load the page.

## Configuration

All settings live at the top of `app.py`:

| Variable | Purpose |
|---|---|
| `PLUGIN_DIR` | Folder with custom Streamlink plugins |
| `FFMPEG_PATH` | Full path to `ffmpeg.exe` |
| `MAX_STREAMS` | Max number of simultaneous streams (default 15) |
| `RETRY_STREAMS` / `RETRY_MAX` | Streamlink retry settings (seconds) |
| `HOST` | `127.0.0.1` = only this machine. `0.0.0.0` = reachable from your local network (anyone on it could start streams on your PC) |

## Troubleshooting

- **"streamlink not found in PATH"** — install Streamlink and make sure it's callable from a terminal.
- **"ffmpeg not found at: ..."** — fix `FFMPEG_PATH` in `app.py`.
- **"Could not generate the first video segment in time"** — the source URL may be offline, invalid, or blocked; check the per-stream logs (**Show Logs**) for details.
- **"Limit of N simultaneous streams reached"** — close a stream before opening another, or raise `MAX_STREAMS`.
