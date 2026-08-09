#!/usr/bin/env python3
"""MCP server for RustDesk headless client.

Talks to the rustdesk-headless binary via a TCP loopback socket
(line-delimited JSON-RPC, shared-token auth). Works on Linux, macOS
and Windows. Exposes tools that let Claude see and control a remote
machine over the RustDesk protocol (E2EE, NAT traversal, codecs).
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import sys
import tempfile
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from fastmcp import FastMCP
from fastmcp.utilities.types import Image


def _rendezvous_path() -> str:
    """Mirror the daemon's `rendezvous_file_path()` (see headless.rs).

    The daemon writes {version, host, port, token, pid} to this JSON file
    (owner-only on Unix) so the client can locate it and authenticate.
    """
    if env := os.environ.get("RUSTDESK_HEADLESS_RENDEZVOUS"):
        return env
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
        return os.path.join(base, "RustDesk-headless", "daemon.json")
    # Linux prefers XDG_RUNTIME_DIR when set; macOS never sets it.
    if sys.platform.startswith("linux") and (rt := os.environ.get("XDG_RUNTIME_DIR")):
        return os.path.join(rt, "rustdesk-headless", "daemon.json")
    uid = os.geteuid()
    return os.path.join(tempfile.gettempdir(), f"rustdesk-headless-{uid}", "daemon.json")


def _read_rendezvous() -> dict[str, Any]:
    path = _rendezvous_path()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        raise RuntimeError(
            f"rustdesk-headless rendezvous file not found: {path}. "
            f"Is the daemon running?"
        )
    except OSError as e:
        raise RuntimeError(f"cannot read rendezvous file {path}: {e}")
    for field_name in ("port", "token"):
        if field_name not in data:
            raise RuntimeError(f"rendezvous file missing '{field_name}'")
    return data


mcp = FastMCP("rustdesk")


@dataclass
class Rpc:
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    lock: asyncio.Lock

    async def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        rid = str(uuid.uuid4())
        req = {"id": rid, "method": method, "params": params or {}}
        async with self.lock:
            self.writer.write((json.dumps(req) + "\n").encode())
            await self.writer.drain()
            line = await self.reader.readline()
        if not line:
            raise RuntimeError("headless socket closed")
        resp = json.loads(line.decode())
        if "error" in resp:
            raise RuntimeError(resp["error"])
        return resp.get("result", {})


_rpc: Rpc | None = None
_rpc_lock = asyncio.Lock()


async def _authenticate(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, token: str) -> None:
    """Send the mandatory auth handshake as the very first line."""
    req = {"id": "auth", "method": "auth", "params": {"token": token}}
    writer.write((json.dumps(req) + "\n").encode())
    await writer.drain()
    line = await reader.readline()
    if not line:
        raise RuntimeError("daemon closed during auth handshake")
    resp = json.loads(line.decode())
    if resp.get("error"):
        raise RuntimeError(f"daemon auth rejected: {resp['error']}")


async def rpc() -> Rpc:
    global _rpc
    async with _rpc_lock:
        if _rpc is None or _rpc.writer.is_closing():
            rdv = _read_rendezvous()
            host = rdv.get("host", "127.0.0.1")
            port = int(rdv["port"])
            token = rdv["token"]
            # asyncio default StreamReader buffer is 64 KiB; PNG screenshots
            # can be > 1 MiB after base64 → bump it.
            reader, writer = await asyncio.open_connection(
                host, port, limit=64 * 1024 * 1024
            )
            await _authenticate(reader, writer, token)
            _rpc = Rpc(reader, writer, asyncio.Lock())
        return _rpc


MOUSE_TYPE_DOWN = 1
MOUSE_TYPE_UP = 2
MOUSE_TYPE_MOVE = 3
MOUSE_WHEEL = 4

BUTTONS = {"left": 1, "right": 2, "middle": 4}


def _click_mask(button: str, action: str) -> int:
    btn = BUTTONS[button]
    t = {"down": MOUSE_TYPE_DOWN, "up": MOUSE_TYPE_UP}[action]
    return (btn << 3) | t


@mcp.tool()
async def rustdesk_connect(
    peer_id: str,
    password: str = "",
    key: str = "",
    relay_server: str = "",
) -> str:
    """Connect to a remote RustDesk peer.

    peer_id: the numeric RustDesk ID of the target machine.
    password: the peer's access password (or empty if previously saved).
    key: the ID-server public key (for self-hosted hbbs).
    relay_server: custom rendezvous/relay server host (optional).
    """
    r = await rpc()
    res = await r.call(
        "connect",
        {"peer_id": peer_id, "password": password, "key": key, "relay_server": relay_server},
    )
    return f"connected: {res}"


@mcp.tool()
async def rustdesk_disconnect() -> str:
    """Close the current RustDesk session."""
    r = await rpc()
    await r.call("disconnect")
    return "disconnected"


@mcp.tool()
async def rustdesk_screenshot() -> Image:
    """Capture the remote desktop and return it as a PNG image."""
    r = await rpc()
    res = await r.call("screenshot")
    data = base64.b64decode(res["png_b64"])
    return Image(data=data, format="png")


@mcp.tool()
async def rustdesk_move_mouse(x: int, y: int) -> str:
    """Move the mouse cursor to (x, y) on the remote screen (pixel coords)."""
    r = await rpc()
    await r.call("mouse", {"mask": MOUSE_TYPE_MOVE, "x": x, "y": y})
    return f"moved to ({x},{y})"


@mcp.tool()
async def rustdesk_click(
    x: int,
    y: int,
    button: str = "left",
    double: bool = False,
) -> str:
    """Click at (x, y) on the remote screen.

    button: 'left', 'right', or 'middle'.
    double: true for a double-click.
    """
    if button not in BUTTONS:
        raise ValueError(f"unknown button {button!r}")
    r = await rpc()
    await r.call("mouse", {"mask": MOUSE_TYPE_MOVE, "x": x, "y": y})
    clicks = 2 if double else 1
    for _ in range(clicks):
        await r.call("mouse", {"mask": _click_mask(button, "down"), "x": x, "y": y})
        await r.call("mouse", {"mask": _click_mask(button, "up"), "x": x, "y": y})
    return f"{'double-' if double else ''}{button}-clicked at ({x},{y})"


@mcp.tool()
async def rustdesk_scroll(x: int, y: int, dy: int) -> str:
    """Scroll the wheel by dy ticks at (x, y). Negative = up, positive = down."""
    r = await rpc()
    await r.call("mouse", {"mask": (MOUSE_WHEEL << 3) | MOUSE_TYPE_MOVE, "x": 0, "y": dy})
    return f"scrolled dy={dy}"


@mcp.tool()
async def rustdesk_type(text: str) -> str:
    """Type literal text on the remote machine."""
    r = await rpc()
    await r.call("type", {"text": text})
    return f"typed {len(text)} chars"


@mcp.tool()
async def rustdesk_key(
    key: str,
    modifiers: list[str] | None = None,
) -> str:
    """Press a named key on the remote machine.

    key: e.g. 'Return', 'Escape', 'Tab', 'F5', 'ArrowLeft', 'a'.
    modifiers: list of 'Ctrl', 'Alt', 'Shift', 'Meta'.
    """
    r = await rpc()
    await r.call("key", {"key": key, "modifiers": modifiers or []})
    return f"pressed {'+'.join((modifiers or []) + [key])}"


@mcp.tool()
async def rustdesk_status() -> dict[str, Any]:
    """Return the current session status (connected, peer_id, screen size, fps)."""
    r = await rpc()
    return await r.call("status")


# ------------------------------------------------------------------
# Gemini Live "continuous watcher" — pushes RustDesk frames to Gemini
# 2.0 Flash Live in a background task and buffers Gemini's textual
# observations so Claude can poll them between MCP calls.
# ------------------------------------------------------------------

@dataclass
class WatcherState:
    task: asyncio.Task | None = None
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    observations: deque = field(default_factory=lambda: deque(maxlen=200))
    started_at: float = 0.0
    frames_sent: int = 0
    sessions_opened: int = 0
    last_error: str | None = None
    prompt: str = ""
    fps: float = 1.0
    model: str = "gemini-2.0-flash-live-001"


_watcher = WatcherState()


async def _watch_loop(state: WatcherState) -> None:
    """Continuously screenshot via the daemon and push to Gemini Live.

    Gemini Live caps video sessions at ~2 minutes — we transparently
    reconnect when the server closes the WebSocket.
    """
    try:
        from google import genai
        from google.genai import types as gtypes
    except ImportError as e:
        state.last_error = f"google-genai not installed: {e}"
        return

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        state.last_error = "GEMINI_API_KEY not set in environment"
        return

    client = genai.Client(api_key=api_key)
    config = gtypes.LiveConnectConfig(response_modalities=["TEXT"])

    period = 1.0 / max(state.fps, 0.1)

    while not state.stop.is_set():
        try:
            async with client.aio.live.connect(model=state.model, config=config) as session:
                state.sessions_opened += 1
                # Send the system prompt as the first turn so Gemini knows what
                # to look for in the incoming frames.
                if state.prompt:
                    await session.send_client_content(
                        turns=gtypes.Content(role="user", parts=[gtypes.Part(text=state.prompt)]),
                        turn_complete=False,
                    )

                async def push_frames() -> None:
                    while not state.stop.is_set():
                        t0 = time.time()
                        try:
                            r = await rpc()
                            res = await r.call("screenshot")
                            png_b64 = res.get("png_b64")
                            if png_b64:
                                # Re-encode to JPEG for fewer tokens / less bandwidth.
                                jpeg_bytes = _png_b64_to_jpeg(png_b64)
                                await session.send_realtime_input(
                                    media=gtypes.Blob(data=jpeg_bytes, mime_type="image/jpeg")
                                )
                                state.frames_sent += 1
                        except Exception as e:
                            state.last_error = f"frame push error: {e}"
                        dt = time.time() - t0
                        await asyncio.sleep(max(0.0, period - dt))

                async def consume() -> None:
                    async for resp in session.receive():
                        if state.stop.is_set():
                            return
                        if getattr(resp, "text", None):
                            state.observations.append({
                                "ts": time.time(),
                                "text": resp.text,
                            })

                push_task = asyncio.create_task(push_frames())
                consume_task = asyncio.create_task(consume())
                stop_task = asyncio.create_task(state.stop.wait())

                done, pending = await asyncio.wait(
                    {push_task, consume_task, stop_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for t in pending:
                    t.cancel()
                for t in pending:
                    try:
                        await t
                    except (asyncio.CancelledError, Exception):
                        pass
        except Exception as e:
            state.last_error = f"session error: {e}"
            # Backoff before reconnecting.
            await asyncio.sleep(2)


def _png_b64_to_jpeg(png_b64: str) -> bytes:
    from PIL import Image as PILImage
    img = PILImage.open(io.BytesIO(base64.b64decode(png_b64))).convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=70)
    return buf.getvalue()


@mcp.tool()
async def rustdesk_watch_start(
    prompt: str = "Décris ce qui se passe à l'écran. Quand l'utilisateur effectue une action visible (clic, frappe, ouverture d'app), résume la nouvelle situation en 1 phrase.",
    fps: float = 1.0,
    model: str = "gemini-2.0-flash-live-001",
) -> str:
    """Start a continuous Gemini Live watcher on the RustDesk session.

    Frames captured from the active RustDesk peer are streamed to Gemini
    at the given fps. Gemini's textual observations are buffered for
    `rustdesk_watch_observations()` to drain.

    Requires GEMINI_API_KEY in the daemon environment.
    """
    if _watcher.task and not _watcher.task.done():
        return f"watcher already running (frames_sent={_watcher.frames_sent})"
    _watcher.stop.clear()
    _watcher.observations.clear()
    _watcher.frames_sent = 0
    _watcher.sessions_opened = 0
    _watcher.last_error = None
    _watcher.started_at = time.time()
    _watcher.prompt = prompt
    _watcher.fps = fps
    _watcher.model = model
    _watcher.task = asyncio.create_task(_watch_loop(_watcher))
    return f"watcher started (model={model}, fps={fps})"


@mcp.tool()
async def rustdesk_watch_stop() -> str:
    """Stop the Gemini Live watcher."""
    if not _watcher.task or _watcher.task.done():
        return "watcher not running"
    _watcher.stop.set()
    try:
        await asyncio.wait_for(_watcher.task, timeout=5)
    except asyncio.TimeoutError:
        _watcher.task.cancel()
    return f"watcher stopped (frames_sent={_watcher.frames_sent}, sessions={_watcher.sessions_opened})"


@mcp.tool()
async def rustdesk_watch_observations(drain: bool = True) -> dict[str, Any]:
    """Return Gemini's buffered textual observations.

    drain: if true (default), clear the buffer after returning.
    """
    items = list(_watcher.observations)
    if drain:
        _watcher.observations.clear()
    return {
        "running": bool(_watcher.task and not _watcher.task.done()),
        "frames_sent": _watcher.frames_sent,
        "sessions_opened": _watcher.sessions_opened,
        "last_error": _watcher.last_error,
        "since_start_s": round(time.time() - _watcher.started_at, 1) if _watcher.started_at else 0,
        "observations": items,
    }


# ------------------------------------------------------------------
# Local HTTP viewer — exposes the latest decoded frame at
# http://127.0.0.1:<port>/ so the human operator can watch what the
# LLM sees, in real time. Disabled unless `rustdesk_viewer_start()`
# is called or RUSTDESK_VIEWER_PORT is set in the environment.
# ------------------------------------------------------------------

@dataclass
class ViewerState:
    server: Any = None
    port: int = 0
    fps: float = 5.0
    latest_jpeg: bytes = b""
    task: asyncio.Task | None = None
    stop: asyncio.Event = field(default_factory=asyncio.Event)


_viewer = ViewerState()


_INDEX_HTML = """<!doctype html>
<html><head><meta charset=utf-8><title>RustDesk MCP viewer</title>
<style>html,body{margin:0;background:#111;height:100%}
img{display:block;margin:auto;max-width:100vw;max-height:100vh;object-fit:contain}
.s{position:fixed;bottom:6px;right:8px;color:#888;font:12px monospace}</style>
</head><body>
<img id=v src="frame.jpg?t=0">
<div class=s id=s></div>
<script>
const img=document.getElementById('v'),s=document.getElementById('s');
let n=0,t0=performance.now();
async function tick(){
  try{
    const r=await fetch('frame.jpg?t='+Date.now(),{cache:'no-store'});
    if(r.ok){
      const b=await r.blob();
      img.src=URL.createObjectURL(b);
      n++;
      const dt=(performance.now()-t0)/1000;
      s.textContent=`${n} frames · ${(n/dt).toFixed(1)} fps`;
    }
  }catch(e){}
  setTimeout(tick, ${INTERVAL_MS});
}
tick();
</script></body></html>
"""


async def _viewer_loop(state: ViewerState) -> None:
    period = 1.0 / max(state.fps, 0.5)
    while not state.stop.is_set():
        try:
            r = await rpc()
            res = await r.call("screenshot")
            png_b64 = res.get("png_b64")
            if png_b64:
                state.latest_jpeg = _png_b64_to_jpeg(png_b64)
        except Exception:
            pass
        await asyncio.sleep(period)


async def _http_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        request_line = await asyncio.wait_for(reader.readline(), timeout=5)
        line = request_line.decode("latin-1", errors="replace").strip()
        # Drain headers.
        while True:
            h = await asyncio.wait_for(reader.readline(), timeout=5)
            if h in (b"\r\n", b""):
                break
        parts = line.split(" ")
        path = parts[1] if len(parts) >= 2 else "/"
        if path.startswith("/frame.jpg"):
            body = _viewer.latest_jpeg or b""
            hdr = (
                f"HTTP/1.1 200 OK\r\n"
                f"Content-Type: image/jpeg\r\n"
                f"Content-Length: {len(body)}\r\n"
                f"Cache-Control: no-store\r\n\r\n"
            ).encode()
            writer.write(hdr + body)
        elif path in ("/", "/index.html"):
            interval = max(int(1000 / max(_viewer.fps, 0.5)), 50)
            html = _INDEX_HTML.replace("${INTERVAL_MS}", str(interval)).encode()
            hdr = (
                f"HTTP/1.1 200 OK\r\n"
                f"Content-Type: text/html; charset=utf-8\r\n"
                f"Content-Length: {len(html)}\r\n\r\n"
            ).encode()
            writer.write(hdr + html)
        else:
            writer.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")
        await writer.drain()
    except Exception:
        pass
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


@mcp.tool()
async def rustdesk_viewer_start(port: int = 8765, fps: float = 5.0) -> str:
    """Start a local HTTP viewer at http://127.0.0.1:<port>/ that streams
    the latest decoded frame so a human can watch alongside the LLM.

    Bound to 127.0.0.1 only; tunnel via SSH if you want remote access.
    """
    if _viewer.server is not None:
        return f"viewer already running at http://127.0.0.1:{_viewer.port}/"
    _viewer.fps = fps
    _viewer.stop.clear()
    _viewer.server = await asyncio.start_server(_http_handler, "127.0.0.1", port)
    _viewer.port = port
    _viewer.task = asyncio.create_task(_viewer_loop(_viewer))
    return f"viewer at http://127.0.0.1:{port}/ (fps={fps})"


@mcp.tool()
async def rustdesk_viewer_stop() -> str:
    """Stop the local HTTP viewer."""
    if _viewer.server is None:
        return "viewer not running"
    _viewer.stop.set()
    _viewer.server.close()
    await _viewer.server.wait_closed()
    _viewer.server = None
    if _viewer.task:
        _viewer.task.cancel()
    return "viewer stopped"


def _maybe_autostart_viewer() -> None:
    port = os.environ.get("RUSTDESK_VIEWER_PORT")
    if not port:
        return
    try:
        port_n = int(port)
    except ValueError:
        return
    fps = float(os.environ.get("RUSTDESK_VIEWER_FPS", "5"))

    async def _start() -> None:
        try:
            await rustdesk_viewer_start(port_n, fps)  # type: ignore[arg-type]
        except Exception:
            pass

    try:
        loop = asyncio.get_event_loop()
        loop.create_task(_start())
    except RuntimeError:
        pass


if __name__ == "__main__":
    _maybe_autostart_viewer()
    mcp.run()
