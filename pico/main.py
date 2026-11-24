"""TemperatureCamera — Raspberry Pi Pico W edition (MicroPython).

Standalone: reads the MLX90640 over I2C, serves the web UI from flash, and
streams v1 binary frames over a WebSocket — the same contract as the Pi
server (see ../templates/stream-contract.html), so the browser UI works
unchanged: it connects to ws://<pico-ip>/ws/thermal.

Also runs under desktop CPython (demo sensor, Wi-Fi skipped), which is how
the e2e tests exercise it:  python3 main.py   with config.py PORT=8080.
"""

import math
import os
import socket
import select
import hashlib
import _thread
import time

try:
    from time import ticks_ms, ticks_diff, sleep_ms
except ImportError:  # CPython
    def ticks_ms():
        return int(time.monotonic() * 1000)

    def ticks_diff(a, b):
        return a - b

    def sleep_ms(ms):
        time.sleep(ms / 1000.0)

try:
    import ustruct as struct
except ImportError:
    import struct
try:
    import ujson as json
except ImportError:
    import json
try:
    import ubinascii as binascii
except ImportError:
    import binascii

from config import (
    WIFI_SSID,
    WIFI_PASSWORD,
    I2C_FREQ,
    STREAM_FPS,
    SENSOR_REFRESH_HZ,
    PORT,
    FLIP_H,
    FLIP_V,
)

# --- stream contract v1 ---------------------------------------------------------
COLS, ROWS = 32, 24
INVALID_PIXEL = -32768
FLAG_FAULT = 2
HEADER = struct.Struct("<BBHHHIfffIHH")  # 32 bytes
FRAME_BYTES = HEADER.size + COLS * ROWS * 2  # 1,568
WS_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

# --- shared state ----------------------------------------------------------------
settings = {"emissivity": 0.95, "reflectedC": 21.0}
settings_lock = _thread.allocate_lock()
stats = {"seq": 0, "fps": 0.0, "fault": False, "started": ticks_ms()}
stats_lock = _thread.allocate_lock()
clients = {}  # socket -> True
clients_lock = _thread.allocate_lock()
dead = []  # sockets that failed to write; reaped by the main loop


# --- frame sources -----------------------------------------------------------------


class DemoSource:
    """Synthetic field (desktop testing / no sensor)."""

    name = "demo"

    def __init__(self):
        self._t0 = ticks_ms()

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
        t = ticks_diff(ticks_ms(), self._t0) / 1000.0
        seed = int(t * STREAM_FPS) % 89
        temps = []
        for y in range(ROWS):
            ny = (y + 0.5) / ROWS
            for x in range(COLS):
                nx = (x + 0.5) / COLS
                v = 20.2 + (ny - 0.5) * 2.0
                v += self._blob(nx, ny, 0.30, 0.60, 0.30, 0.34, 6.4)
                v += self._blob(nx, ny, 0.45, 0.76, 0.17, 0.19, 5.2)
                v += self._blob(
                    nx, ny,
                    0.655 + math.sin(t * 0.5) * 0.014,
                    0.40 + math.cos(t * 0.37) * 0.012,
                    0.075, 0.105, 16.2,
                )
                v -= self._blob(nx, ny, 0.855, 0.30, 0.15, 0.16, 4.4)
                v += (self._noise(x, y, seed) - 0.5) * 0.85
                temps.append(v)
        return temps


class MLXSource:
    """MLX90640 on the Pico's I2C0 (GP4=SDA, GP5=SCL)."""

    name = "mlx90640"
    RATES = (0.5, 1, 2, 4, 8, 16, 32, 64)

    def __init__(self):
        import board
        import busio
        import adafruit_mlx90640

        i2c = busio.I2C(board.SCL, board.SDA, frequency=I2C_FREQ)
        self._mlx = adafruit_mlx90640.MLX90640(i2c)
        nearest = min(self.RATES, key=lambda r: abs(r - SENSOR_REFRESH_HZ))
        self._mlx.refresh_rate = getattr(
            adafruit_mlx90640.RefreshRate, f"REFRESH_{int(nearest)}_HZ"
        ) if nearest == int(nearest) else getattr(
            adafruit_mlx90640.RefreshRate, "REFRESH_0_5_HZ"
        )
        print("MLX90640 at", I2C_FREQ, "Hz I2C,", nearest, "Hz refresh")
        try:
            self._mlx.emissivity = settings["emissivity"]
        except Exception:
            pass

    def read(self):
        frame = [0.0] * (COLS * ROWS)
        last = None
        for _ in range(3):  # transient "math domain error" from glitched reads
            try:
                self._mlx.getFrame(frame)
                return frame
            except (ValueError, RuntimeError) as e:
                last = e
                sleep_ms(50)
        raise last


def make_source():
    try:
        return MLXSource()
    except Exception as e:
        print("sensor init failed, using demo source:", e)
        return DemoSource()


# --- encoding ------------------------------------------------------------------------


def apply_orientation(temps, flip_h, flip_v):
    if not flip_h and not flip_v:
        return temps
    grid = [temps[r * COLS : (r + 1) * COLS] for r in range(ROWS)]
    if flip_v:
        grid.reverse()
    if flip_h:
        grid = [row[::-1] for row in grid]
    return [v for row in grid for v in row]


def encode_frame(seq, sensor_ms, ambient_c, emissivity, temps, fault=False):
    buf = bytearray(FRAME_BYTES)
    mn = float("inf")
    mx = -float("inf")
    off = HEADER.size
    for v in temps:
        if v is None or v != v:  # NaN marks a pixel with no valid reading
            s = INVALID_PIXEL
        else:
            s = int(round(v * 100))
            if s > 32767:
                s = 32767
            elif s < -32767:
                s = -32767
            c = s / 100.0
            if c < mn:
                mn = c
            if c > mx:
                mx = c
        struct.pack_into("<h", buf, off, s)
        off += 2
    if mn == float("inf"):
        mn = mx = 0.0
        fault = True
    HEADER.pack_into(
        buf, 0,
        1,  # version
        FLAG_FAULT if fault else 0,
        HEADER.size, COLS, ROWS,
        seq & 0xFFFFFFFF,
        ambient_c, mn, mx,
        sensor_ms & 0xFFFFFFFF,
        max(0, min(1000, int(round(emissivity * 1000)))),
        0,
    )
    return bytes(buf)


# --- frame production (core 1) --------------------------------------------------------


def frame_loop(source):
    started = ticks_ms()
    seq = 0
    count = 0
    window_start = started
    while True:
        t0 = ticks_ms()
        try:
            temps = source.read()
            fault = False
        except Exception as e:
            print("sensor read failed, sending fault frame:", e)
            temps = [float("nan")] * (COLS * ROWS)
            fault = True
        temps = apply_orientation(temps, FLIP_H, FLIP_V)
        with settings_lock:
            ambient = settings["reflectedC"]
            emissivity = settings["emissivity"]
        seq += 1
        data = encode_frame(seq, ticks_diff(ticks_ms(), started), ambient, emissivity, temps, fault)
        with clients_lock:
            targets = list(clients.keys())
        for c in targets:
            try:
                ws_send_binary(c, data)
            except OSError:
                with clients_lock:
                    clients.pop(c, None)
                dead.append(c)
        with stats_lock:
            stats["seq"] = seq
            stats["fault"] = fault
        count += 1
        now = ticks_ms()
        if ticks_diff(now, window_start) >= 1000:
            with stats_lock:
                stats["fps"] = count * 1000.0 / ticks_diff(now, window_start)
            count = 0
            window_start = now
        wait = int(1000.0 / STREAM_FPS) - ticks_diff(ticks_ms(), t0)
        if wait > 0:
            sleep_ms(wait)


# --- WebSocket helpers -----------------------------------------------------------------


def ws_send(conn, opcode, payload):
    n = len(payload)
    if n < 126:
        hdr = struct.pack("!BB", 0x80 | opcode, n)
    else:
        hdr = struct.pack("!BBH", 0x80 | opcode, 126, n)
    conn.sendall(hdr + payload)


def ws_send_binary(conn, data):
    ws_send(conn, 0x2, data)


def ws_send_text(conn, text):
    ws_send(conn, 0x1, text.encode())


def _recv_exact(conn, n):
    buf = b""
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


def ws_read_frame(conn):
    """One client frame (socket must have a timeout). Returns (opcode, payload) or None."""
    try:
        hdr = _recv_exact(conn, 2)
        if hdr is None:
            return None
        op = hdr[0] & 0x0F
        ln = hdr[1] & 0x7F
        if ln == 126:
            ext = _recv_exact(conn, 2)
            if ext is None:
                return None
            ln = struct.unpack("!H", ext)[0]
        elif ln == 127:
            ext = _recv_exact(conn, 8)
            if ext is None:
                return None
            ln = struct.unpack("!Q", ext)[0]
        mask = None
        if hdr[1] & 0x80:
            mask = _recv_exact(conn, 4)
            if mask is None:
                return None
        payload = _recv_exact(conn, ln) if ln else b""
        if payload is None:
            return None
        if mask:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        return op, payload
    except OSError:
        return None


def handle_control(payload):
    """Returns a reply dict, or None to stay silent (contract §10)."""
    try:
        msg = json.loads(payload)
    except Exception:
        return None
    if not isinstance(msg, dict):
        return None
    mtype = msg.get("type")
    if mtype == "ping":
        return {"type": "pong", "id": msg.get("id")}
    if mtype == "settings":
        changes = {}
        try:
            if "emissivity" in msg:
                e = float(msg["emissivity"])
                if not 0.0 < e <= 1.0:
                    return {"type": "status", "state": "error",
                            "detail": f"emissivity must be in (0, 1], got {e}"}
                changes["emissivity"] = e
            if "reflectedC" in msg:
                changes["reflectedC"] = float(msg["reflectedC"])
        except (TypeError, ValueError):
            return None
        with settings_lock:
            settings.update(changes)
        return {"type": "status", "state": "ok", "detail": "settings applied"}
    return None


# --- HTTP + WS server (core 0) ----------------------------------------------------------


def send_response(conn, status, content_type, body, close=True):
    conn.sendall(
        b"HTTP/1.1 " + status + b"\r\nContent-Type: " + content_type +
        b"\r\nContent-Length: " + str(len(body)).encode() +
        b"\r\nConnection: close\r\n\r\n" + body
    )


def serve_file(conn, path, content_type):
    try:
        size = os.stat(path)[6]
    except OSError:
        send_response(conn, b"404 Not Found", b"text/plain", b"not found")
        return
    conn.sendall(
        b"HTTP/1.1 200 OK\r\nContent-Type: " + content_type +
        b"\r\nContent-Length: " + str(size).encode() +
        b"\r\nConnection: close\r\n\r\n"
    )
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1024)
            if not chunk:
                break
            conn.sendall(chunk)


def status_json():
    with stats_lock:
        snap = dict(stats)
    with settings_lock:
        s = dict(settings)
    with clients_lock:
        n = len(clients)
    return json.dumps({
        "source": source.name,
        "demo": source.name == "demo",
        "clients": n,
        "seq": snap["seq"],
        "fps": round(snap["fps"], 1),
        "fault": snap["fault"],
        "frameBytes": FRAME_BYTES,
        "uptimeS": round(ticks_diff(ticks_ms(), snap["started"]) / 1000.0, 1),
        "settings": s,
    }).encode()


def read_request(conn):
    conn.settimeout(2)
    data = b""
    while b"\r\n\r\n" not in data and len(data) < 8192:
        try:
            chunk = conn.recv(1024)
        except OSError:
            break
        if not chunk:
            break
        data += chunk
    return data


def _fd_of(s):
    # CPython's poll() returns fd ints; MicroPython returns the registered object
    return s if isinstance(s, int) else s.fileno()


_poller = None
_by_fd = {}


def _register(sock, events):
    _poller.register(sock, events)
    _by_fd[_fd_of(sock)] = sock


def _unregister(sock):
    try:
        _poller.unregister(sock)
    except Exception:
        pass
    _by_fd.pop(_fd_of(sock), None)


def handle_http(conn):
    data = read_request(conn)
    if not data:
        conn.close()
        return
    line = data.split(b"\r\n", 1)[0]
    parts = line.split()
    if len(parts) < 2:
        conn.close()
        return
    path = parts[1].decode()
    headers = data.lower()

    if path == "/ws/thermal" and b"upgrade: websocket" in headers:
        key = None
        for h in data.split(b"\r\n"):
            if h.lower().startswith(b"sec-websocket-key:"):
                key = h.split(b":", 1)[1].strip()
        if key is None:
            conn.close()
            return
        accept = binascii.b2a_base64(hashlib.sha1(key + WS_GUID).digest()).strip()
        conn.sendall(
            b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
            b"Connection: Upgrade\r\nSec-WebSocket-Accept: " + accept + b"\r\n\r\n"
        )
        conn.settimeout(0.5)
        with clients_lock:
            clients[conn] = True
        _register(conn, select.POLLIN)
        print("ws client connected:", len(clients))
        return

    if path in ("/", "/index.html"):
        serve_file(conn, "index.html", b"text/html; charset=utf-8")
    elif path == "/stream-contract.html":
        serve_file(conn, "stream-contract.html", b"text/html; charset=utf-8")
    elif path == "/api/status":
        send_response(conn, b"200 OK", b"application/json", status_json())
    else:
        send_response(conn, b"404 Not Found", b"text/plain", b"not found")
    conn.close()


def serve():
    global _poller
    lsock = socket.socket()
    lsock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    lsock.bind(("0.0.0.0", PORT))
    lsock.listen(4)
    lsock.setblocking(False)
    _poller = select.poll()
    _register(lsock, select.POLLIN)
    print("listening on port", PORT)
    while True:
        for s, _ev in _poller.poll(250):
            obj = _by_fd.get(_fd_of(s))
            if obj is None:
                continue
            if obj is lsock:
                conn, _addr = lsock.accept()
                try:
                    handle_http(conn)
                except OSError:
                    conn.close()
            else:
                # ws client sent a control frame (or dropped)
                frame = ws_read_frame(obj)
                if frame is None:
                    _unregister(obj)
                    with clients_lock:
                        clients.pop(obj, None)
                    obj.close()
                    print("ws client gone:", len(clients))
                    continue
                op, payload = frame
                if op == 0x8:  # close
                    _unregister(obj)
                    with clients_lock:
                        clients.pop(obj, None)
                    obj.close()
                elif op == 0x9:  # ping -> pong
                    ws_send(obj, 0xA, payload)
                elif op == 0x1:  # text control message
                    reply = handle_control(payload.decode())
                    if reply is not None:
                        ws_send_text(obj, json.dumps(reply))
        while dead:
            s = dead.pop()
            _unregister(s)
            try:
                s.close()
            except Exception:
                pass


def wifi_connect():
    try:
        import network
    except ImportError:
        print("no network module (desktop mode), skipping Wi-Fi")
        return
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    if not wlan.isconnected():
        print("connecting to Wi-Fi...")
        wlan.connect(WIFI_SSID, WIFI_PASSWORD)
        t0 = ticks_ms()
        while not wlan.isconnected():
            if ticks_diff(ticks_ms(), t0) > 20000:
                raise RuntimeError("Wi-Fi connect timeout")
            sleep_ms(200)
    print("Wi-Fi up:", wlan.ifconfig()[0])


source = None


def main():
    global source
    wifi_connect()
    source = make_source()
    _thread.start_new_thread(frame_loop, (source,))
    serve()


main()
