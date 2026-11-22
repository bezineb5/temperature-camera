"""TemperatureCamera server.

Serves the web UI and streams raw MLX90640 temperature frames to the browser
over a binary WebSocket using stream contract v1 (see /stream-contract.html).
Palette, range, unit, overlays, snapshots and recording are client-side by
design; the server only sends °C frames.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import queue
import signal
import struct
import sys
import threading
import time
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from pathlib import Path
from types import FrameType
from typing import Any, Optional, Protocol

from flask import Flask, Response, jsonify, render_template
from flask_sock import Sock
from simple_websocket import Server

logger = logging.getLogger(__name__)

app = Flask(__name__)
sock = Sock(app)

# --- Stream contract v1 (mirrors templates/stream-contract.html) ------------
# version, flags, headerBytes, columns, rows, sequence,
# ambientC, frameMinC, frameMaxC, sensorMs, emissivity, reserved
HEADER = struct.Struct("<BBHHHIfffIHH")  # 32 bytes, little-endian, no padding
PROTO_VERSION = 1
COLS, ROWS = 32, 24  # MLX90640
INVALID_PIXEL = -32768  # int16 marker for a pixel with no valid reading
FLAG_FLOAT32 = 1
FLAG_FAULT = 2
FRAME_BYTES = HEADER.size + COLS * ROWS * 2  # 1,568 bytes

DEFAULT_FPS = 12  # default stream rate (contract §12)


@dataclass
class SensorSettings:
    """Physical model applied on the Pi before encoding (contract §6, §8)."""

    emissivity: float = 0.95
    reflectedC: float = 21.0

    def __post_init__(self) -> None:
        self.emissivity = float(self.emissivity)
        self.reflectedC = float(self.reflectedC)
        if not 0.0 < self.emissivity <= 1.0:
            raise ValueError(f"emissivity must be in (0, 1], got {self.emissivity}")


def encode_frame(
    seq: int,
    sensor_ms: int,
    ambient_c: float,
    emissivity: float,
    temps: Iterable[float],
    fault: bool = False,
) -> bytes:
    """Encode one v1 frame: 32-byte header + int16 hundredths-of-°C payload."""
    pixels: list[int] = []
    mn, mx = math.inf, -math.inf
    for v in temps:
        if v is None or not math.isfinite(v):
            pixels.append(INVALID_PIXEL)
            continue
        scaled = round(v * 100)
        scaled = max(-32767, min(32767, scaled))
        pixels.append(scaled)
        c = scaled / 100.0
        mn = min(mn, c)
        mx = max(mx, c)
    if mn is math.inf:  # no valid pixels at all
        mn = mx = 0.0
        fault = True
    header = HEADER.pack(
        PROTO_VERSION,
        FLAG_FAULT if fault else 0,
        HEADER.size,
        COLS,
        ROWS,
        seq & 0xFFFFFFFF,
        float(ambient_c),
        mn,
        mx,
        sensor_ms & 0xFFFFFFFF,
        max(0, min(1000, round(emissivity * 1000))),
        0,
    )
    return header + struct.pack(f"<{len(pixels)}h", *pixels)


# --- Frame sources -----------------------------------------------------------


class FrameSource(Protocol):
    """Anything that yields 32x24 °C frames and carries physical-model settings."""

    name: str
    settings: SensorSettings

    def read(self) -> list[float]: ...


class DemoSource:
    """Synthetic 32x24 field for development off the Pi. Yields °C like the sensor."""

    name = "demo"

    def __init__(self) -> None:
        self._t0: float = time.monotonic()
        self.settings: SensorSettings = SensorSettings()

    @staticmethod
    def _blob(
        nx: float, ny: float, cx: float, cy: float, rx: float, ry: float, amp: float
    ) -> float:
        dx = (nx - cx) / rx
        dy = (ny - cy) / ry
        return amp * math.exp(-(dx * dx + dy * dy))

    @staticmethod
    def _noise(x: int, y: int, seed: int) -> float:
        n = math.sin(x * 12.9898 + y * 78.233 + seed * 37.719) * 43758.5453
        return n - math.floor(n)

    def read(self) -> list[float]:
        t = time.monotonic() - self._t0
        seed = int(t * DEFAULT_FPS) % 89
        temps: list[float] = []
        for y in range(ROWS):
            ny = (y + 0.5) / ROWS
            for x in range(COLS):
                nx = (x + 0.5) / COLS
                v = 20.2 + (ny - 0.5) * 2.0
                v += self._blob(nx, ny, 0.30, 0.60, 0.30, 0.34, 6.4)
                v += self._blob(nx, ny, 0.45, 0.76, 0.17, 0.19, 5.2)
                v += self._blob(
                    nx,
                    ny,
                    0.655 + math.sin(t * 0.5) * 0.014,
                    0.40 + math.cos(t * 0.37) * 0.012,
                    0.075,
                    0.105,
                    16.2,
                )
                v -= self._blob(nx, ny, 0.855, 0.30, 0.15, 0.16, 4.4)
                v += (self._noise(x, y, seed) - 0.5) * 0.85
                temps.append(v)
        return temps


class MLX90640Source:
    """Real MLX90640 sensor on a Raspberry Pi. getFrame returns calibrated °C."""

    name = "mlx90640"

    RATES: dict[float, str] = {
        0.5: "REFRESH_0_5_HZ",
        1: "REFRESH_1_HZ",
        2: "REFRESH_2_HZ",
        4: "REFRESH_4_HZ",
        8: "REFRESH_8_HZ",
        16: "REFRESH_16_HZ",
        32: "REFRESH_32_HZ",
        64: "REFRESH_64_HZ",
    }

    def __init__(self, target_hz: float = 16.0) -> None:
        import adafruit_mlx90640
        import board

        self._mlx: Any = adafruit_mlx90640.MLX90640(board.I2C())
        nearest: float = min(self.RATES, key=lambda r: abs(r - target_hz))
        self._mlx.refresh_rate = getattr(
            adafruit_mlx90640.RefreshRate, self.RATES[nearest]
        )
        logger.info(f"MLX90640 refresh rate set to {nearest} Hz")
        self.settings = SensorSettings()
        self._apply_emissivity(self.settings.emissivity)

    def _apply_emissivity(self, value: float) -> None:
        try:
            self._mlx.emissivity = value
        except (AttributeError, ValueError) as e:
            logger.warning(f"Could not set emissivity on sensor: {e}")

    def read(self) -> list[float]:
        frame: list[float] = [0.0] * (COLS * ROWS)
        self._mlx.getFrame(frame)  # raises ValueError/RuntimeError on bad reads
        return frame


source: FrameSource | None = None
source_lock = threading.Lock()


def apply_settings(update: SettingsUpdate) -> StatusMessage:
    """Apply a settings control message atomically; returns the StatusMessage reply."""
    with source_lock:
        assert source is not None
        changes: dict[str, float] = {
            key: value
            for key, value in (
                ("emissivity", update.emissivity),
                ("reflectedC", update.reflectedC),
            )
            if value is not None
        }
        try:
            candidate = replace(source.settings, **changes)
        except ValueError as e:
            logger.warning(f"Rejected settings update: {e}")
            return StatusMessage(state="error", detail=str(e))
        source.settings = candidate
        if isinstance(source, MLX90640Source) and "emissivity" in changes:
            source._apply_emissivity(candidate.emissivity)
        logger.info(f"Settings updated: {source.settings}")
        return StatusMessage(state="ok", detail="settings applied")


# --- Control channel messages (contract §8) ------------------------------------


@dataclass
class Ping:
    """Client -> Pi liveness check."""

    id: Any = None


@dataclass
class SettingsUpdate:
    """Client -> Pi physical-model update; None means 'leave unchanged'."""

    emissivity: Optional[float] = None
    reflectedC: Optional[float] = None


@dataclass
class Pong:
    """Pi -> client reply to a Ping."""

    type: str = "pong"
    id: Any = None

    def to_json(self) -> str:
        return json.dumps(asdict(self))


@dataclass
class StatusMessage:
    """Pi -> client human-readable state."""

    type: str = "status"
    state: str = "ok"
    detail: str = ""

    def to_json(self) -> str:
        return json.dumps(asdict(self))


def parse_control(raw: str) -> Ping | SettingsUpdate | None:
    """Parse one text control message into a dataclass.

    Returns None for malformed JSON, non-dict payloads, invalid values and
    unknown types — all ignored without a reply (contract §10).
    """
    try:
        data: Any = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    mtype = data.get("type")
    if mtype == "ping":
        return Ping(id=data.get("id"))
    if mtype == "settings":
        try:
            return SettingsUpdate(
                emissivity=float(data["emissivity"]) if "emissivity" in data else None,
                reflectedC=float(data["reflectedC"]) if "reflectedC" in data else None,
            )
        except (TypeError, ValueError):
            return None
    return None


# --- Fan-out hub --------------------------------------------------------------


class StreamHub:
    """Broadcasts encoded frames to connected WebSocket clients.

    Per contract §7 a congested client drops the newest frame, never queues.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._queues: set[queue.Queue[bytes | None]] = set()

    def add(self) -> queue.Queue[bytes | None]:
        q: queue.Queue[bytes | None] = queue.Queue(maxsize=2)
        with self._lock:
            self._queues.add(q)
        return q

    def remove(self, q: queue.Queue[bytes | None]) -> None:
        with self._lock:
            self._queues.discard(q)
        try:
            q.put_nowait(None)  # wake the sender thread so it can exit
        except queue.Full:
            pass

    def broadcast(self, data: bytes) -> None:
        with self._lock:
            queues = list(self._queues)
        for q in queues:
            try:
                q.put_nowait(data)
            except queue.Full:
                pass  # drop newest for this client

    def client_count(self) -> int:
        with self._lock:
            return len(self._queues)


hub = StreamHub()


@dataclass
class StreamStats:
    """Mutable stream counters, guarded by stats_lock."""

    seq: int = 0
    fps: float = 0.0
    fault: bool = False
    started: float = field(default_factory=time.time)


stats = StreamStats()
stats_lock = threading.Lock()


def stream_worker(src: FrameSource, stop_event: threading.Event, fps: float) -> None:
    """Read the sensor, encode v1 frames, broadcast them at `fps`."""
    seq = 0
    started = time.monotonic()
    frame_count = 0
    window_start = started
    interval = 1.0 / fps
    while not stop_event.is_set():
        loop_start = time.monotonic()
        fault = False
        temps: list[float]
        try:
            temps = src.read()
        except Exception as e:
            logger.warning(f"Sensor read failed, sending fault frame: {e}")
            fault = True
            temps = [math.nan] * (COLS * ROWS)

        with source_lock:
            settings: SensorSettings = replace(src.settings)  # consistent snapshot

        seq += 1
        sensor_ms = int((loop_start - started) * 1000)
        hub.broadcast(
            encode_frame(
                seq,
                sensor_ms,
                settings.reflectedC,
                settings.emissivity,
                temps,
                fault=fault,
            )
        )

        frame_count += 1
        now = time.monotonic()
        if now - window_start >= 1.0:
            with stats_lock:
                stats.seq = seq
                stats.fps = frame_count / (now - window_start)
                stats.fault = fault
            frame_count = 0
            window_start = now

        elapsed = time.monotonic() - loop_start
        stop_event.wait(max(0.0, interval - elapsed))


# --- HTTP + WebSocket routes ---------------------------------------------------


@app.route("/")
def index() -> str:
    """Live view UI."""
    return render_template("index.html")


@app.route("/stream-contract.html")
def stream_contract() -> str:
    """The v1 stream contract the client and server implement."""
    return render_template("stream-contract.html")


@dataclass
class StatusPayload:
    """Body of GET /api/status."""

    source: Optional[str]
    demo: bool
    clients: int
    seq: int
    fps: float
    fault: bool
    frameBytes: int
    uptimeS: float
    settings: dict[str, float] | None


@app.route("/api/status")
def api_status() -> Response:
    """Server health: source, clients, frame stats, current settings."""
    with stats_lock:
        snap = replace(stats)
    with source_lock:
        payload = StatusPayload(
            source=source.name if source is not None else None,
            demo=source is not None and source.name == "demo",
            clients=hub.client_count(),
            seq=snap.seq,
            fps=round(snap.fps, 1),
            fault=snap.fault,
            frameBytes=FRAME_BYTES,
            uptimeS=round(time.time() - snap.started, 1),
            settings=asdict(source.settings) if source is not None else None,
        )
    return jsonify(asdict(payload))


@sock.route("/ws/thermal")
def thermal_ws(ws: Server) -> None:
    """Binary v1 frames out, JSON text control messages in (contract §2, §8)."""
    q = hub.add()
    logger.info(f"WebSocket client connected ({hub.client_count()} total)")

    def sender() -> None:
        try:
            while True:
                data = q.get()
                if data is None:
                    break
                ws.send(data)
        except Exception:
            pass

    threading.Thread(target=sender, daemon=True).start()

    try:
        while True:
            msg = ws.receive()
            if msg is None:
                break
            if isinstance(msg, (bytes, bytearray)):
                continue  # pixel data never travels client -> server in v1
            ctrl = parse_control(msg)
            if ctrl is None:
                continue  # malformed or unknown: ignored without a reply (contract §10)
            if isinstance(ctrl, Ping):
                ws.send(Pong(id=ctrl.id).to_json())
            elif isinstance(ctrl, SettingsUpdate):
                ws.send(apply_settings(ctrl).to_json())
    finally:
        hub.remove(q)
        logger.info(f"WebSocket client disconnected ({hub.client_count()} total)")


def main() -> None:
    global source

    parser = argparse.ArgumentParser(description="Thermal Camera Streaming Server")
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Stream a synthetic thermal field instead of the MLX90640 (development off the Pi)",
    )
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument(
        "--fps",
        type=float,
        default=DEFAULT_FPS,
        help=f"Stream rate in frames per second (default {DEFAULT_FPS})",
    )
    args = parser.parse_args()

    log_handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    try:
        log_dir = Path("logs")
        log_dir.mkdir(exist_ok=True)
        file_handler = logging.FileHandler(
            log_dir
            / f"temperature_camera_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
        )
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
        )
        log_handlers.append(file_handler)
    except Exception as e:
        print(f"Warning: Could not set up file logging: {e}")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=log_handlers,
    )

    logger.info("Starting TemperatureCamera server")
    logger.info(f"Python version: {sys.version}")
    logger.info(f"Working directory: {Path.cwd()}")

    stop_event = threading.Event()

    def signal_handler(signum: int, frame: FrameType | None) -> None:
        logger.info("Received shutdown signal, stopping threads...")
        stop_event.set()
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    if args.demo:
        source = DemoSource()
        logger.info("Demo mode: streaming a synthetic thermal field")
    else:
        try:
            source = MLX90640Source()
        except Exception as e:
            logger.error(f"Failed to initialize MLX90640: {e}")
            logger.error(
                "Hint: run with --demo to stream a synthetic field without hardware"
            )
            sys.exit(1)

    worker = threading.Thread(
        target=stream_worker, args=(source, stop_event, args.fps), daemon=True
    )
    worker.start()

    try:
        app.run(host=args.host, port=args.port, threaded=True)
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received")
    finally:
        stop_event.set()
        worker.join(timeout=5.0)


if __name__ == "__main__":
    main()
