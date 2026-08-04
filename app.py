"""
Streamlink Manager - Web Version (HLS)
----------------------------------------------
Instead of opening VLC, this script starts a local web server.
Open http://localhost:5000 in your browser, paste the stream URL
and the video plays right on the page.
"""

import os
import time
import uuid
import shutil
import atexit
import tempfile
import threading
import subprocess
from urllib.parse import urlparse

from flask import Flask, request, Response, render_template_string, send_from_directory, jsonify

app = Flask(__name__)

# ---- Settings (tweak if needed) --------------------------------------------
# Folder with custom Streamlink plugins, next to this script
PLUGIN_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "custom_plugin")
RETRY_STREAMS = "30"
RETRY_MAX = "300"

# Full path to ffmpeg.exe
FFMPEG_PATH = r"C:\ffmpeg\bin\ffmpeg.exe"

# Temporary base folder for all active streams
BASE_HLS_DIR = os.path.join(tempfile.gettempdir(), "streamlink_web_hls")

# Max number of simultaneous streams (avoids accidentally exhausting CPU/bandwidth)
MAX_STREAMS = 15

# By default the server only accepts connections from this machine. Change to
# "0.0.0.0" if you want to access it from other devices on your local network
# (in that case, anyone on the network could start streams on your PC).
HOST = "127.0.0.1"

# Dict holding the processes for each active stream (key = stream_id)
_active_streams = {}
_lock = threading.Lock()


def _clear_all_folders():
    try:
        shutil.rmtree(BASE_HLS_DIR, ignore_errors=True)
    except Exception:
        pass
    os.makedirs(BASE_HLS_DIR, exist_ok=True)


# clean up orphaned folders from a previous run that didn't shut down
# cleanly (e.g. server killed with Ctrl+C in the middle of a stream)
_clear_all_folders()
atexit.register(_clear_all_folders)

os.makedirs(PLUGIN_DIR, exist_ok=True)


def _streams_watchdog():
    """
    Runs in the background periodically checking whether any streamlink/ffmpeg
    process died on its own (e.g. streamer went offline). When that happens,
    keeps the entry around for a few cycles with alive=False (so the frontend
    can poll /status and show the message), then removes everything and frees
    up the slot in MAX_STREAMS.
    """
    dead_since = {}
    while True:
        time.sleep(3)
        with _lock:
            for sid, info in list(_active_streams.items()):
                alive = (info["streamlink"].poll() is None) or (info["ffmpeg"].poll() is None)
                info["alive"] = alive

                if not alive:
                    dead_since.setdefault(sid, time.time())
                    # give the frontend 15s to notice the death before
                    # deleting the files and removing the stream
                    if time.time() - dead_since[sid] > 15:
                        try:
                            info["streamlink"].kill()
                            info["ffmpeg"].kill()
                        except Exception:
                            pass
                        shutil.rmtree(info["dir"], ignore_errors=True)
                        del _active_streams[sid]
                        dead_since.pop(sid, None)
                        print(f">>> Stream [{sid}] died on its own and was cleaned up.")
                else:
                    dead_since.pop(sid, None)


threading.Thread(target=_streams_watchdog, daemon=True).start()


def _log_stderr(proc, name):
    # streamlink/ffmpeg dump a lot of irrelevant progress/info lines;
    # only worth showing what looks like a real error/warning
    try:
        for line in iter(proc.stderr.readline, b""):
            if not line:
                continue
            text = line.decode(errors='replace').rstrip()
            if any(p in text.lower() for p in ("error", "warn", "fail")):
                print(f"[{name}] {text}")
    except Exception:
        pass


HTML_PAGE = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Streamlink Web Player</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/hls.js/1.5.13/hls.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Sortable/1.15.2/Sortable.min.js"></script>
<style>
    :root {
        --bg: #0c0c0e;
        --bg-panel: #18181b;
        --bg-panel-2: #202024;
        --border: #2a2a2e;
        --border-hover: #3a3a40;
        --text: #eaeaea;
        --text-dim: #8f8f96;
        --accent: #5b8def;
        --accent-hover: #75a0f5;
        --green: #2ecc71;
        --yellow: #f1c40f;
        --red: #e74c3c;
        --radius: 10px;
    }

    * { box-sizing: border-box; }

    body {
        font-family: -apple-system, "Segoe UI", Roboto, Arial, sans-serif;
        background: var(--bg);
        color: var(--text);
        text-align: center;
        margin: 0;
        padding-bottom: 40px;
    }

    ::-webkit-scrollbar { width: 10px; height: 10px; }
    ::-webkit-scrollbar-track { background: transparent; }
    ::-webkit-scrollbar-thumb { background: var(--border-hover); border-radius: 5px; }

    .top-bar {
        position: sticky;
        top: 0;
        z-index: 10;
        background: rgba(12,12,14,0.9);
        backdrop-filter: blur(8px);
        border-bottom: 1px solid var(--border);
        padding: 28px 20px 22px;
    }

    .top-container {
        max-width: 900px;
        margin: 0 auto;
    }

    .top-container h1 {
        margin: 0 0 6px;
        font-size: 26px;
        font-weight: 600;
        letter-spacing: -0.3px;
    }
    .top-container h1 .accent { color: var(--accent); }

    .top-container p {
        margin: 0 0 18px;
        color: var(--text-dim);
        font-size: 14px;
    }

    .input-row {
        display: flex;
        gap: 10px;
        max-width: 640px;
        margin: 0 auto;
    }

    input[type=text] {
        flex: 1;
        padding: 11px 14px;
        font-size: 15px;
        background: var(--bg-panel-2);
        border: 1px solid var(--border);
        color: var(--text);
        border-radius: 8px;
        transition: border-color .15s, box-shadow .15s;
    }
    input[type=text]:focus {
        outline: none;
        border-color: var(--accent);
        box-shadow: 0 0 0 3px rgba(91,141,239,0.18);
    }
    input[type=text]::placeholder { color: var(--text-dim); }

    button {
        padding: 11px 22px;
        font-size: 15px;
        font-weight: 600;
        cursor: pointer;
        background: var(--accent);
        color: #0c0c0e;
        border: none;
        border-radius: 8px;
        transition: background .15s, transform .1s;
    }
    button:hover { background: var(--accent-hover); }
    button:active { transform: scale(0.97); }

    #playersContainer {
        display: flex;
        flex-wrap: wrap;
        gap: 20px;
        justify-content: center;
        max-width: 1400px;
        margin: 30px auto 0 auto;
        padding: 0 20px;
    }

    #playersContainer:empty::after {
        content: "No active streams. Paste a URL above to get started.";
        color: var(--text-dim);
        font-size: 14px;
        padding: 70px 20px;
        width: 100%;
    }

    .stream-container {
        background: var(--bg-panel);
        border: 1px solid var(--border);
        box-shadow: 0 2px 10px rgba(0,0,0,0.25);
        padding: 18px;
        border-radius: var(--radius);
        text-align: left;
        transition: border-color .15s;

        /* Freely resizable (horizontal and vertical) */
        resize: both;
        overflow: auto;

        /* Force exactly 2 items per row (calc with the 20px gap) */
        flex: 1 1 calc(50% - 10px);
        min-width: 350px;
        max-width: calc(50% - 10px);
        min-height: 300px;
        height: auto;
    }
    .stream-container:hover { border-color: var(--border-hover); }

    @media (max-width: 768px) {
        .stream-container {
            flex: 1 1 100%;
            max-width: 100%;
        }
        .input-row { flex-direction: column; }
    }

    .stream-header {
        font-weight: 600;
        color: var(--text);
        margin-bottom: 12px;
        word-break: break-all;
        display: flex;
        justify-content: space-between;
        align-items: center;
        gap: 10px;
        cursor: grab;
        user-select: none;
    }
    .stream-header:active {
        cursor: grabbing;
    }

    .header-info {
        display: flex;
        align-items: center;
        gap: 8px;
        overflow: hidden;
        font-size: 13px;
        color: var(--text-dim);
    }

    .header-controls {
        display: flex;
        align-items: center;
        gap: 6px;
        flex-shrink: 0;
    }

    /* Visual status indicator (dot) */
    .status-dot {
        width: 9px;
        height: 9px;
        border-radius: 50%;
        background-color: #666;
        display: inline-block;
        flex-shrink: 0;
        box-shadow: 0 0 5px rgba(0,0,0,0.5);
    }
    .status-dot.green { background-color: var(--green); box-shadow: 0 0 8px var(--green); }
    .status-dot.yellow { background-color: var(--yellow); box-shadow: 0 0 8px var(--yellow); animation: pulse 1.2s ease-in-out infinite; }
    .status-dot.red { background-color: var(--red); box-shadow: 0 0 8px var(--red); }

    @keyframes pulse {
        0%, 100% { opacity: 1; }
        50% { opacity: 0.35; }
    }

    .btn-action {
        border: none;
        padding: 5px 9px;
        cursor: pointer;
        border-radius: 6px;
        font-size: 11px;
        font-weight: 600;
        color: white;
        transition: background .15s;
    }
    .btn-reload { background: #2563a8; }
    .btn-reload:hover { background: #3498db; }
    .btn-close { background: #a83232; }
    .btn-close:hover { background: #e74c3c; }
    .btn-blur { background: var(--bg-panel-2); border: 1px solid var(--border); color: var(--text-dim); }
    .btn-blur:hover { background: var(--border); color: var(--text); }
    .btn-blur.active { background: var(--accent); border-color: var(--accent); color: #0c0c0e; }

    .status {
        margin-top: 10px;
        color: var(--text-dim);
        font-size: 13px;
        min-height: 20px;
        white-space: pre-wrap;
        background: var(--bg);
        border: 1px solid var(--border);
        padding: 10px;
        border-radius: 6px;
        display: none;
        max-height: 150px;
        overflow-y: auto;
    }

    video {
        margin-top: 15px;
        width: 100%;
        height: auto;
        max-height: 70vh;
        background: #000;
        border-radius: 6px;
        display: block;
        transition: filter .15s;
    }
    video.blurred {
        filter: blur(24px);
    }

    .toggle-log-btn {
        background: var(--bg-panel-2);
        color: var(--text-dim);
        border: 1px solid var(--border);
        padding: 5px 10px;
        font-size: 12px;
        cursor: pointer;
        margin-top: 10px;
        border-radius: 6px;
        transition: background .15s, color .15s;
    }
    .toggle-log-btn:hover { background: var(--border); color: var(--text); }

    .sortable-ghost {
        opacity: 0.3;
    }
</style>
</head>
<body>
    <header class="top-bar">
        <div class="top-container">
            <h1>Streamlink <span class="accent">Web</span></h1>
            <p>Paste the stream URL and press Enter. Drag the card header to reorder or use the bottom-right corner to resize.</p>
            <div class="input-row">
                <input type="text" id="urlInput" placeholder="Paste the stream URL here...">
                <button onclick="startStream()">Watch</button>
            </div>
        </div>
    </header>

    <div id="playersContainer"></div>

<script>
document.addEventListener("DOMContentLoaded", () => {
    // Drag-and-drop reordering
    new Sortable(document.getElementById('playersContainer'), {
        animation: 150,
        handle: '.stream-header',
        ghostClass: 'sortable-ghost',
        onEnd: () => saveLocalState()
    });

    // Enter shortcut on the URL input
    document.getElementById('urlInput').addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
            startStream();
        }
    });

    // Restore previously saved streams
    restoreLocalState();

    // Chrome pauses video-only autoplay in background tabs to save power
    // (AbortError "power saving"); resume automatically when the tab regains focus
    document.addEventListener("visibilitychange", () => {
        if (document.hidden) return;
        document.querySelectorAll('#playersContainer video').forEach(v => {
            if (v.paused) v.play().catch(() => {});
        });
    });
});

function saveLocalState() {
    const containers = document.querySelectorAll('.stream-container');
    const urls = [];
    containers.forEach(c => {
        if (c.dataset.streamUrl) {
            urls.push(c.dataset.streamUrl);
        }
    });
    localStorage.setItem('streamlink_active_urls', JSON.stringify(urls));
}

async function restoreLocalState() {
    const saved = localStorage.getItem('streamlink_active_urls');
    if (!saved) return;
    try {
        const urls = JSON.parse(saved);
        // Restore in reverse order to keep the same sequence with prepend
        for (let i = urls.length - 1; i >= 0; i--) {
            await createStreamInDom(urls[i], false);
        }
    } catch (e) {
        console.error("Error restoring previous session:", e);
    }
}

async function startStream() {
    const urlInput = document.getElementById('urlInput');
    const url = urlInput.value.trim();

    if (!url) {
        alert("Enter a valid URL.");
        return;
    }

    await createStreamInDom(url, true);
    urlInput.value = '';
}

async function createStreamInDom(url, saveToLocalStorage = true) {
    const container = document.getElementById('playersContainer');
    const domId = 'stream_' + Math.random().toString(36).substr(2, 9);

    const wrapper = document.createElement('div');
    wrapper.className = 'stream-container';
    wrapper.id = `wrapper_${domId}`;
    wrapper.dataset.streamUrl = url;
    wrapper.innerHTML = `
        <div class="stream-header">
            <div class="header-info">
                <span class="status-dot red" id="dot_${domId}" title="Disconnected"></span>
                <span style="overflow: hidden; text-overflow: ellipsis; white-space: nowrap;" title="${url}">Stream: ${url}</span>
            </div>
            <div class="header-controls">
                <button class="btn-action btn-blur" onclick="toggleBlur('${domId}')" title="Blur Video">Blur</button>
                <button class="btn-action btn-reload" onclick="reloadStream('${domId}')" title="Reload Player">Reload</button>
                <button class="btn-action btn-close" onclick="closeStream('${domId}')" title="Close Stream">Close</button>
            </div>
        </div>
        <video id="video_${domId}" controls autoplay muted playsinline></video>
        <button class="toggle-log-btn" onclick="toggleLog('${domId}')">Show Logs</button>
        <div id="status_${domId}" class="status"></div>
    `;
    container.prepend(wrapper);

    if (saveToLocalStorage) {
        saveLocalState();
    }

    const statusEl = document.getElementById(`status_${domId}`);
    const video = document.getElementById(`video_${domId}`);
    const dotEl = document.getElementById(`dot_${domId}`);

    function setStatusDot(color, tooltip) {
        dotEl.className = `status-dot ${color}`;
        dotEl.title = tooltip;
    }

    function log(msg) {
        const line = document.createElement('div');
        line.innerText = new Date().toLocaleTimeString() + " - " + msg;
        statusEl.appendChild(line);
        console.log(`[${domId}] ${msg}`);
    }

    log("Starting streamlink + ffmpeg... (this can take a few seconds)");
    setStatusDot('yellow', 'Starting stream...');

    let data;
    try {
        const resp = await fetch('/start?url=' + encodeURIComponent(url));
        data = await resp.json();
    } catch (err) {
        log("Error contacting the server: " + err);
        setStatusDot('red', 'Server connection error');
        return;
    }

    if (!data.ok) {
        log("Failed: " + data.error);
        setStatusDot('red', 'Failed: ' + data.error);
        return;
    }

    wrapper.dataset.serverStreamId = data.stream_id;
    startMonitoring(domId, data.stream_id);

    log("Loading player...");
    const hlsUrl = data.hls_url + "?t=" + Date.now();

    if (window.Hls && Hls.isSupported()) {
        const hls = new Hls({
            liveSyncDurationCount: 3,
            maxLiveSyncPlaybackRate: 1.2,
            debug: false,
            backBufferLength: 30, // pure live playback, not a DVR: cap RAM instead of buffering forever
        });
        hls.loadSource(hlsUrl);
        hls.attachMedia(video);

        hls.on(Hls.Events.MANIFEST_PARSED, (_, d) => {
            log("Manifest loaded, levels: " + d.levels.length);

            video.play().catch(err => {
                if (!(err && err.name === 'AbortError' && /power/i.test(err.message))) {
                    log("play() rejected: " + err);
                }
            });
        });

        hls.on(Hls.Events.ERROR, (_, d) => {
            log("HLS event: " + d.type + " / " + d.details + (d.fatal ? " (FATAL)" : ""));
            if (d.fatal) {
                setStatusDot('red', 'Fatal error in HLS stream');
                switch (d.type) {
                    case Hls.ErrorTypes.NETWORK_ERROR:
                        hls.startLoad();
                        break;
                    case Hls.ErrorTypes.MEDIA_ERROR:
                        hls.recoverMediaError();
                        break;
                    default:
                        hls.destroy();
                        break;
                }
            }
        });

        wrapper.hlsInstance = hls;
    } else {
        log("This browser does not support HLS.js.");
        setStatusDot('red', 'Browser without HLS support');
    }

    video.addEventListener('loadedmetadata', () => log("Metadata loaded: " + video.videoWidth + "x" + video.videoHeight));
    video.addEventListener('playing', () => {
        log("Video playing");
        setStatusDot('green', 'Playing');
    });
    video.addEventListener('waiting', () => {
        log("Waiting for more data (buffering)...");
        setStatusDot('yellow', 'Loading (Buffering)...');
    });
    video.addEventListener('stalled', () => {
        log("Stream download stalled");
        setStatusDot('yellow', 'Unstable (Stalled)');
    });

    video.addEventListener('error', () => {
        const err = video.error;
        const errInfo = err ? `{code: ${err.code}, message: "${err.message}"}` : "unknown";
        log("Error on the <video> element: " + errInfo);
        setStatusDot('red', 'Error on the video element');
    });
}

async function reloadStream(domId) {
    const wrapper = document.getElementById(`wrapper_${domId}`);
    if (!wrapper) return;
    const url = wrapper.dataset.streamUrl;
    if (!url) return;

    // Destroy the current HLS instance if any
    if (wrapper.hlsInstance) {
        wrapper.hlsInstance.destroy();
        wrapper.hlsInstance = null;
    }

    const video = document.getElementById(`video_${domId}`);
    if (video) {
        video.pause();
        video.src = "";
    }

    const serverStreamId = wrapper.dataset.serverStreamId;
    if (serverStreamId) {
        try {
            await fetch('/stop?stream_id=' + serverStreamId);
        } catch (e) {}
    }

    // Restart the flow for this same card
    const statusEl = document.getElementById(`status_${domId}`);
    const dotEl = document.getElementById(`dot_${domId}`);

    function setStatusDot(color, tooltip) {
        dotEl.className = `status-dot ${color}`;
        dotEl.title = tooltip;
    }

    function log(msg) {
        const line = document.createElement('div');
        line.innerText = new Date().toLocaleTimeString() + " - " + msg;
        statusEl.appendChild(line);
        console.log(`[${domId}] ${msg}`);
    }

    log("Restarting stream...");
    setStatusDot('yellow', 'Restarting...');

    let data;
    try {
        const resp = await fetch('/start?url=' + encodeURIComponent(url));
        data = await resp.json();
    } catch (err) {
        log("Error contacting the server: " + err);
        setStatusDot('red', 'Connection error');
        return;
    }

    if (!data.ok) {
        log("Failed: " + data.error);
        setStatusDot('red', 'Failed: ' + data.error);
        return;
    }

    wrapper.dataset.serverStreamId = data.stream_id;
    startMonitoring(domId, data.stream_id);
    const hlsUrl = data.hls_url + "?t=" + Date.now();

    if (window.Hls && Hls.isSupported()) {
        const hls = new Hls({
            liveSyncDurationCount: 3,
            maxLiveSyncPlaybackRate: 1.2,
            debug: false,
            backBufferLength: 30, // pure live playback, not a DVR: cap RAM instead of buffering forever
        });
        hls.loadSource(hlsUrl);
        hls.attachMedia(video);

        hls.on(Hls.Events.MANIFEST_PARSED, (_, d) => {
            log("Manifest reloaded");
            video.play().catch(err => {
                if (!(err && err.name === 'AbortError' && /power/i.test(err.message))) {
                    log("play() rejected: " + err);
                }
            });
        });

        wrapper.hlsInstance = hls;
    }
}

function startMonitoring(domId, streamId) {
    const wrapper = document.getElementById(`wrapper_${domId}`);
    if (!wrapper) return;

    if (wrapper.statusInterval) {
        clearInterval(wrapper.statusInterval);
    }

    wrapper.statusInterval = setInterval(async () => {
        const w = document.getElementById(`wrapper_${domId}`);
        if (!w) {
            clearInterval(wrapper.statusInterval);
            return;
        }

        let data;
        try {
            const resp = await fetch('/status?stream_id=' + streamId);
            data = await resp.json();
        } catch (e) {
            return; // one-off network failure, retry next cycle
        }

        if (!data.exists || data.alive === false) {
            const statusEl = document.getElementById(`status_${domId}`);
            const dotEl = document.getElementById(`dot_${domId}`);
            if (statusEl) {
                const line = document.createElement('div');
                line.innerText = new Date().toLocaleTimeString() + " - Stream ended (streamer offline or source lost).";
                statusEl.appendChild(line);
            }
            if (dotEl) {
                dotEl.className = 'status-dot red';
                dotEl.title = 'Stream ended';
            }
            if (w.hlsInstance) {
                w.hlsInstance.destroy();
                w.hlsInstance = null;
            }
            clearInterval(w.statusInterval);
        }
    }, 5000);
}

async function closeStream(domId) {
    const wrapper = document.getElementById(`wrapper_${domId}`);
    if (!wrapper) return;

    if (wrapper.statusInterval) {
        clearInterval(wrapper.statusInterval);
    }

    if (wrapper.hlsInstance) {
        wrapper.hlsInstance.destroy();
    }

    const video = document.getElementById(`video_${domId}`);
    if (video) {
        video.pause();
        video.src = "";
    }

    const serverStreamId = wrapper.dataset.serverStreamId;
    if (serverStreamId) {
        try {
            await fetch('/stop?stream_id=' + serverStreamId);
        } catch (e) {}
    }

    wrapper.remove();
    saveLocalState();
}

function toggleBlur(domId) {
    const video = document.getElementById(`video_${domId}`);
    const btn = event.target;
    if (!video) return;
    video.classList.toggle('blurred');
    btn.classList.toggle('active', video.classList.contains('blurred'));
}

function toggleLog(domId) {
    const statusEl = document.getElementById(`status_${domId}`);
    const btn = event.target;
    if (statusEl.style.display === "block") {
        statusEl.style.display = "none";
        btn.innerText = "Show Logs";
    } else {
        statusEl.style.display = "block";
        btn.innerText = "Hide Logs";
    }
}
</script>
</body>
</html>
"""


@app.route('/')
def index():
    return render_template_string(HTML_PAGE)


def _check_dependencies():
    if not shutil.which("streamlink"):
        return "streamlink not found in PATH."
    if not os.path.isfile(FFMPEG_PATH):
        return f"ffmpeg not found at: {FFMPEG_PATH} (adjust FFMPEG_PATH in the code)."
    return None


@app.route('/start')
def start():
    url = request.args.get('url')
    if not url:
        return jsonify(ok=False, error="URL not provided"), 400
    if urlparse(url).scheme not in ("http", "https"):
        return jsonify(ok=False, error="Invalid URL (use http:// or https://)"), 400

    dep_error = _check_dependencies()
    if dep_error:
        return jsonify(ok=False, error=dep_error)

    with _lock:
        # reuse an already active stream with the same URL instead of
        # duplicating streamlink/ffmpeg processes wasting CPU/bandwidth
        for sid, info in _active_streams.items():
            if info["url"] == url and info.get("alive", True):
                return jsonify(
                    ok=True,
                    stream_id=sid,
                    hls_url=f"/hls/{sid}/{info['manifest_name']}",
                    reused=True,
                )

        if len(_active_streams) >= MAX_STREAMS:
            return jsonify(
                ok=False,
                error=f"Limit of {MAX_STREAMS} simultaneous streams reached. Close one before opening another.",
            )

    stream_id = str(uuid.uuid4())[:8]
    stream_dir = os.path.join(BASE_HLS_DIR, stream_id)
    os.makedirs(stream_dir, exist_ok=True)

    streamlink_cmd = [
        "streamlink",
        "--plugin-dirs", PLUGIN_DIR,
        url,
        "best",
        "--retry-streams", RETRY_STREAMS,
        "--retry-max", RETRY_MAX,
        "--stdout",
    ]

    manifest_name = "stream.m3u8"
    manifest_path = os.path.join(stream_dir, manifest_name)
    ffmpeg_cmd = [
        FFMPEG_PATH,
        "-i", "pipe:0",
        "-c", "copy",
        "-f", "hls",
        "-hls_time", "2",
        "-hls_list_size", "6",
        "-hls_flags", "delete_segments+append_list+omit_endlist+independent_segments",
        "-hls_segment_filename", os.path.join(stream_dir, "seg_%05d.ts"),
        manifest_path,
    ]

    print(f"\n>>> Starting stream [{stream_id}] for: {url}")
    streamlink_proc = subprocess.Popen(streamlink_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    ffmpeg_proc = subprocess.Popen(ffmpeg_cmd, stdin=streamlink_proc.stdout, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    streamlink_proc.stdout.close()  # without this, streamlink won't get SIGPIPE if ffmpeg dies

    threading.Thread(target=_log_stderr, args=(streamlink_proc, f"streamlink-{stream_id}"), daemon=True).start()
    threading.Thread(target=_log_stderr, args=(ffmpeg_proc, f"ffmpeg-{stream_id}"), daemon=True).start()

    with _lock:
        _active_streams[stream_id] = {
            "streamlink": streamlink_proc,
            "ffmpeg": ffmpeg_proc,
            "dir": stream_dir,
            "url": url,
            "manifest_name": manifest_name,
            "alive": True,
        }

    deadline = time.time() + 20
    ok = False
    while time.time() < deadline:
        if streamlink_proc.poll() is not None and ffmpeg_proc.poll() is not None:
            break
        if os.path.exists(manifest_path) and os.path.getsize(manifest_path) > 0:
            with open(manifest_path, "r", errors="ignore") as f:
                content = f.read()
            if ".ts" in content:
                ok = True
                break
        time.sleep(0.4)

    if not ok:
        with _lock:
            if stream_id in _active_streams:
                try:
                    streamlink_proc.kill()
                    ffmpeg_proc.kill()
                except Exception:
                    pass
                del _active_streams[stream_id]
        shutil.rmtree(stream_dir, ignore_errors=True)

        return jsonify(
            ok=False,
            error="Could not generate the first video segment in time.",
        )

    return jsonify(
        ok=True,
        stream_id=stream_id,
        hls_url=f"/hls/{stream_id}/{manifest_name}",
    )


@app.route('/stop')
def stop_stream():
    stream_id = request.args.get('stream_id')
    if not stream_id:
        return jsonify(ok=False), 400

    with _lock:
        if stream_id in _active_streams:
            info = _active_streams[stream_id]
            for proc in (info["ffmpeg"], info["streamlink"]):
                if proc and proc.poll() is None:
                    try:
                        proc.kill()
                    except Exception:
                        pass
            shutil.rmtree(info["dir"], ignore_errors=True)
            del _active_streams[stream_id]
            print(f">>> Stream [{stream_id}] stopped and cleaned up.")

    return jsonify(ok=True)


@app.route('/status')
def status_stream():
    stream_id = request.args.get('stream_id')
    if not stream_id:
        return jsonify(exists=False), 400

    with _lock:
        info = _active_streams.get(stream_id)
        if not info:
            return jsonify(exists=False)
        alive = info.get("alive", True)

    return jsonify(exists=True, alive=alive)


@app.route('/hls/<stream_id>/<path:filename>')
def hls_files(stream_id, filename):
    with _lock:
        info = _active_streams.get(stream_id)
        if not info:
            return "Stream not found", 404
        stream_dir = info["dir"]

    response = send_from_directory(stream_dir, filename)
    response.headers["Cache-Control"] = "no-cache"
    if filename.endswith(".m3u8"):
        response.headers["Content-Type"] = "application/vnd.apple.mpegurl"
    elif filename.endswith(".ts"):
        response.headers["Content-Type"] = "video/mp2t"
    return response


if __name__ == '__main__':
    print("--- Streamlink Web Server Started ---")
    print(f"Open http://{HOST}:5000 in your browser.\n")
    if HOST == "0.0.0.0":
        print("WARNING: the server is accessible from any device on your local network.\n")
    app.run(host=HOST, port=5000, threaded=True)
