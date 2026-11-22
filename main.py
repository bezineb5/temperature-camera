"""TemperatureCamera server.

Serves the web UI and streams raw MLX90640 temperature frames to the browser
over a binary WebSocket using stream contract v1 (see /stream-contract.html).
Palette, range, unit, overlays, snapshots and recording are client-side by
design; the server only sends °C frames.
"""

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
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, render_template
from flask_sock import Sock

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


def encode_frame(seq, sensor_ms, ambient_c, emissivity, temps, fault=False):
    """Encode one v1 frame: 32-byte header + int16 hundredths-of-°C payload."""
    pixels = []
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


class DemoSource:
    """Synthetic 32x24 field for development off the Pi. Yields °C like the sensor."""

    name = "demo"

    def __init__(self):
        self._t0 = time.monotonic()
        self.settings = {"emissivity": 0.95, "reflectedC": 21.0}

    @staticmethod
    def _blob(nx, ny, cx, cy, rx, ry, amp):
        dx = (nx - cx) / rx
        dy = (ny - cy) / ry
        return amp * math.exp(-(dx * dx + dy * dy))

    @staticmethod
    def _noise(x, y, seed):
        n = math.sin(x * 12.9898 + y * 78.233 + seed * 37.719) * 43758.5453
        return n - math.floor(n)

    def read(self):
        t = time.monotonic() - self._t0
        seed = int(t * DEFAULT_FPS) % 89
        temps = []
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

    RATES = {
        0.5: "REFRESH_0_5_HZ",
        1: "REFRESH_1_HZ",
        2: "REFRESH_2_HZ",
        4: "REFRESH_4_HZ",
        8: "REFRESH_8_HZ",
        16: "REFRESH_16_HZ",
        32: "REFRESH_32_HZ",
        64: "REFRESH_64_HZ",
    }

    def __init__(self, target_hz=16.0):
        import adafruit_mlx90640
        import board

        self._mlx = adafruit_mlx90640.MLX90640(board.I2C())
        nearest = min(self.RATES, key=lambda r: abs(r - target_hz))
        self._mlx.refresh_rate = getattr(
            adafruit_mlx90640.RefreshRate, self.RATES[nearest]
        )
        logger.info(f"MLX90640 refresh rate set to {nearest} Hz")
        self.settings = {"emissivity": 0.95, "reflectedC": 21.0}
        self._apply_emissivity(self.settings["emissivity"])

    def _apply_emissivity(self, value):
        try:
            self._mlx.emissivity = value
        except (AttributeError, ValueError) as e:
            logger.warning(f"Could not set emissivity on sensor: {e}")

    def read(self):
        frame = [0.0] * (COLS * ROWS)
        self._mlx.getFrame(frame)  # raises ValueError/RuntimeError on bad reads
        return frame


source = None
source_lock = threading.Lock()


def apply_settings(ctrl):
    """Handle a {"type":"settings", ...} control message (contract §8)."""
    global source
    with source_lock:
        if "emissivity" in ctrl:
            try:
                e = float(ctrl["emissivity"])
                if 0.0 < e <= 1.0:
                    source.settings["emissivity"] = e
                    if isinstance(source, MLX90640Source):
                        source._apply_emissivity(e)
            except (TypeError, ValueError):
                pass
        if "reflectedC" in ctrl:
            try:
                source.settings["reflectedC"] = float(ctrl["reflectedC"])
            except (TypeError, ValueError):
                pass
        logger.info(f"Settings updated: {source.settings}")


# --- Fan-out hub --------------------------------------------------------------


class StreamHub:
    """Broadcasts encoded frames to connected WebSocket clients.

    Per contract §7 a congested client drops the newest frame, never queues.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._queues = set()

    def add(self):
        q = queue.Queue(maxsize=2)
        with self._lock:
            self._queues.add(q)
        return q

    def remove(self, q):
        with self._lock:
            self._queues.discard(q)
        try:
            q.put_nowait(None)  # wake the sender thread so it can exit
        except queue.Full:
            pass

    def broadcast(self, data):
        with self._lock:
            queues = list(self._queues)
        for q in queues:
            try:
                q.put_nowait(data)
            except queue.Full:
                pass  # drop newest for this client

    def client_count(self):
        with self._lock:
            return len(self._queues)


hub = StreamHub()
stats = {"seq": 0, "fps": 0.0, "fault": False, "started": time.time()}
stats_lock = threading.Lock()


def stream_worker(src, stop_event, fps):
    """Read the sensor, encode v1 frames, broadcast them at `fps`."""
    seq = 0
    started = time.monotonic()
    frame_count = 0
    window_start = started
    interval = 1.0 / fps
    while not stop_event.is_set():
        loop_start = time.monotonic()
        fault = False
        try:
            temps = src.read()
        except Exception as e:
            logger.warning(f"Sensor read failed, sending fault frame: {e}")
            fault = True
            temps = [math.nan] * (COLS * ROWS)

        with source_lock:
            ambient = src.settings["reflectedC"]
            emissivity = src.settings["emissivity"]

        seq += 1
        sensor_ms = int((loop_start - started) * 1000)
        hub.broadcast(
            encode_frame(seq, sensor_ms, ambient, emissivity, temps, fault=fault)
        )

        frame_count += 1
        now = time.monotonic()
        if now - window_start >= 1.0:
            with stats_lock:
                stats.update(
                    seq=seq, fps=frame_count / (now - window_start), fault=fault
                )
            frame_count = 0
            window_start = now

        elapsed = time.monotonic() - loop_start
        stop_event.wait(max(0.0, interval - elapsed))


# --- HTTP + WebSocket routes ---------------------------------------------------


@app.route("/")
def index():
    """Live view UI."""
    return render_template("index.html")


@app.route("/stream-contract.html")
def stream_contract():
    """The v1 stream contract the client and server implement."""
    return render_template("stream-contract.html")


@app.route("/api/status")
def api_status():
    """Server health: source, clients, frame stats, current settings."""
    with stats_lock:
        snap = dict(stats)
    with source_lock:
        source_name = source.name if source else None
        settings = dict(source.settings) if source else None
    return jsonify(
        {
            "source": source_name,
            "demo": source_name == "demo",
            "clients": hub.client_count(),
            "seq": snap["seq"],
            "fps": round(snap["fps"], 1),
            "fault": snap["fault"],
            "frameBytes": FRAME_BYTES,
            "uptimeS": round(time.time() - snap["started"], 1),
            "settings": settings,
        }
    )


@sock.route("/ws/thermal")
def thermal_ws(ws):
    """Binary v1 frames out, JSON text control messages in (contract §2, §8)."""
    q = hub.add()
    logger.info(f"WebSocket client connected ({hub.client_count()} total)")

    def sender():
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
            try:
                ctrl = json.loads(msg)
            except (TypeError, ValueError):
                continue
            mtype = ctrl.get("type")
            if mtype == "ping":
                ws.send(json.dumps({"type": "pong", "id": ctrl.get("id")}))
            elif mtype == "settings":
                apply_settings(ctrl)
                ws.send(
                    json.dumps(
                        {"type": "status", "state": "ok", "detail": "settings applied"}
                    )
                )
            # unknown types are ignored without a reply (contract §10)
    finally:
        hub.remove(q)
        logger.info(f"WebSocket client disconnected ({hub.client_count()} total)")


def main():
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

    log_handlers = [logging.StreamHandler(sys.stdout)]
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

    def signal_handler(signum, frame):
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
