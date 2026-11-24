"""Unit tests for the TemperatureCamera server (no hardware needed)."""

import json
import math
import queue
import struct
import threading
import time

import pytest

import main
from main import (
    FLAG_FAULT,
    HEADER,
    INVALID_PIXEL,
    DemoSource,
    MLX90640Source,
    Ping,
    SensorSettings,
    SettingsUpdate,
    StatusMessage,
    StreamHub,
    apply_orientation,
    apply_settings,
    encode_frame,
    parse_control,
    stream_worker,
)

# geometry comes from the source interface, not module constants
_DEMO = DemoSource()
COLS, ROWS = _DEMO.cols, _DEMO.rows
FRAME_BYTES = _DEMO.frame_bytes


def decode_frame(data: bytes) -> dict:
    """Decode a v1 frame per the stream contract (mirrors the client decoder)."""
    (
        version,
        flags,
        header_bytes,
        cols,
        rows,
        seq,
        ambient,
        mn,
        mx,
        sensor_ms,
        emis,
        _,
    ) = HEADER.unpack_from(data)
    pixels = struct.unpack_from(f"<{cols * rows}h", data, header_bytes)
    return {
        "version": version,
        "flags": flags,
        "headerBytes": header_bytes,
        "cols": cols,
        "rows": rows,
        "seq": seq,
        "ambientC": ambient,
        "minC": mn,
        "maxC": mx,
        "sensorMs": sensor_ms,
        "emissivity": emis / 1000,
        "pixels": pixels,
    }


# --- encode_frame -------------------------------------------------------------


def test_encode_frame_layout_and_header():
    data = encode_frame(7, 1234, 21.4, 0.95, [20.0] * (COLS * ROWS), COLS, ROWS)
    assert len(data) == FRAME_BYTES == 1568
    f = decode_frame(data)
    assert f["version"] == 1
    assert f["flags"] == 0
    assert f["headerBytes"] == 32
    assert (f["cols"], f["rows"]) == (32, 24)
    assert f["seq"] == 7
    assert f["ambientC"] == pytest.approx(21.4)
    assert f["minC"] == f["maxC"] == pytest.approx(20.0)
    assert f["sensorMs"] == 1234
    assert f["emissivity"] == pytest.approx(0.95)


def test_encode_frame_payload_is_hundredths_int16():
    temps = [20.0] * (COLS * ROWS)
    temps[0] = 21.43
    temps[1] = -5.67
    data = encode_frame(1, 0, 21.0, 0.95, temps, COLS, ROWS)
    f = decode_frame(data)
    assert f["pixels"][0] == 2143
    assert f["pixels"][1] == -567
    assert f["minC"] == pytest.approx(-5.67)
    assert f["maxC"] == pytest.approx(21.43)


def test_encode_frame_invalid_pixels_excluded_from_min_max():
    temps = [25.0] * (COLS * ROWS)
    temps[0] = math.nan
    temps[1] = None
    temps[2] = 100.0  # hot outlier must not leak into min
    data = encode_frame(1, 0, 21.0, 0.95, temps, COLS, ROWS)
    f = decode_frame(data)
    assert f["pixels"][0] == INVALID_PIXEL == -32768
    assert f["pixels"][1] == INVALID_PIXEL
    assert f["minC"] == pytest.approx(25.0)
    assert f["maxC"] == pytest.approx(100.0)


def test_encode_frame_clamps_to_int16_range():
    data = encode_frame(
        1, 0, 21.0, 0.95, [400.0, -400.0] + [20.0] * (COLS * ROWS - 2), COLS, ROWS
    )
    f = decode_frame(data)
    assert f["pixels"][0] == 32767
    assert f["pixels"][1] == -32767
    assert f["maxC"] == pytest.approx(327.67)
    assert f["minC"] == pytest.approx(-327.67)


def test_encode_frame_sequence_wraps_at_2_to_32():
    data = encode_frame(2**32 + 5, 0, 21.0, 0.95, [20.0] * (COLS * ROWS), COLS, ROWS)
    assert decode_frame(data)["seq"] == 5


def test_encode_frame_all_invalid_sets_fault_flag():
    data = encode_frame(1, 0, 21.0, 0.95, [math.nan] * (COLS * ROWS), COLS, ROWS)
    f = decode_frame(data)
    assert f["flags"] & FLAG_FAULT
    assert f["minC"] == f["maxC"] == 0.0


def test_encode_frame_explicit_fault_flag():
    data = encode_frame(
        1, 0, 21.0, 0.95, [20.0] * (COLS * ROWS), COLS, ROWS, fault=True
    )
    assert decode_frame(data)["flags"] & FLAG_FAULT


def test_encode_frame_rejects_wrong_pixel_count():
    with pytest.raises(ValueError, match="pixels"):
        encode_frame(1, 0, 21.0, 0.95, [20.0] * 10, COLS, ROWS)


def test_frame_source_carries_geometry():
    assert (DemoSource.cols, DemoSource.rows) == (32, 24)
    assert (MLX90640Source.cols, MLX90640Source.rows) == (32, 24)
    assert DemoSource().frame_bytes == 1568
    assert DemoSource().name == "demo"
    assert MLX90640Source.name == "mlx90640"


# --- apply_orientation ------------------------------------------------------------


def test_apply_orientation():
    temps = [1, 2, 3, 4, 5, 6]  # 3 cols x 2 rows: [1,2,3] / [4,5,6]
    assert apply_orientation(temps, 3, 2, False, False) == temps
    assert apply_orientation(temps, 3, 2, True, False) == [3, 2, 1, 6, 5, 4]
    assert apply_orientation(temps, 3, 2, False, True) == [4, 5, 6, 1, 2, 3]
    assert apply_orientation(temps, 3, 2, True, True) == [6, 5, 4, 3, 2, 1]


# --- MLX90640Source retry behavior -------------------------------------------------


class _FakeMLX:
    def __init__(self, failures: int):
        self.calls = 0
        self.failures = failures

    def getFrame(self, frame) -> None:
        self.calls += 1
        if self.calls <= self.failures:
            raise ValueError("math domain error")
        for i in range(len(frame)):
            frame[i] = 21.0


def _bare_mlx_source(fake) -> MLX90640Source:
    src = MLX90640Source.__new__(MLX90640Source)  # skip hardware init
    src._mlx = fake
    return src


def test_mlx_source_retries_transient_math_domain_error():
    fake = _FakeMLX(failures=2)
    temps = _bare_mlx_source(fake).read()
    assert temps == [21.0] * (32 * 24)
    assert fake.calls == 3


def test_mlx_source_raises_after_retries_exhausted():
    fake = _FakeMLX(failures=99)
    with pytest.raises(ValueError, match="math domain error"):
        _bare_mlx_source(fake).read()
    assert fake.calls == 3


# --- SensorSettings -------------------------------------------------------------


def test_settings_defaults_and_validation():
    s = SensorSettings()
    assert s.emissivity == 0.95
    assert s.reflectedC == 21.0
    with pytest.raises(ValueError):
        SensorSettings(emissivity=0.0)
    with pytest.raises(ValueError):
        SensorSettings(emissivity=1.5)
    assert SensorSettings(emissivity=1.0).emissivity == 1.0


# --- parse_control ---------------------------------------------------------------


def test_parse_ping():
    msg = parse_control(json.dumps({"type": "ping", "id": 7}))
    assert isinstance(msg, Ping)
    assert msg.id == 7
    assert parse_control(json.dumps({"type": "ping"})).id is None


def test_parse_settings():
    msg = parse_control(
        json.dumps({"type": "settings", "emissivity": 0.9, "reflectedC": 22})
    )
    assert isinstance(msg, SettingsUpdate)
    assert msg.emissivity == 0.9
    assert msg.reflectedC == 22.0
    partial = parse_control(json.dumps({"type": "settings", "reflectedC": 20.0}))
    assert partial.emissivity is None


@pytest.mark.parametrize(
    "raw",
    [
        "{not json",
        "[1, 2]",
        '"just a string"',
        json.dumps({"type": "bogus"}),
        json.dumps({"type": "settings", "emissivity": "abc"}),
        json.dumps({"no_type": True}),
    ],
)
def test_parse_control_ignores_garbage(raw):
    assert parse_control(raw) is None


# --- apply_settings ---------------------------------------------------------------


@pytest.fixture
def demo_source(monkeypatch):
    src = DemoSource()
    monkeypatch.setattr(main, "source", src)
    return src


def test_apply_settings_valid_update(demo_source):
    reply = apply_settings(SettingsUpdate(emissivity=0.9, reflectedC=22.5))
    assert isinstance(reply, StatusMessage)
    assert reply.state == "ok"
    assert demo_source.settings.emissivity == 0.9
    assert demo_source.settings.reflectedC == 22.5
    assert json.loads(reply.to_json()) == {
        "type": "status",
        "state": "ok",
        "detail": "settings applied",
    }


def test_apply_settings_partial_update_keeps_other_field(demo_source):
    apply_settings(SettingsUpdate(reflectedC=30.0))
    assert demo_source.settings.reflectedC == 30.0
    assert demo_source.settings.emissivity == 0.95


def test_apply_settings_invalid_is_atomic(demo_source):
    reply = apply_settings(SettingsUpdate(emissivity=5.0, reflectedC=30.0))
    assert reply.state == "error"
    assert "emissivity" in reply.detail
    # rejected as a whole: reflectedC must not have changed either
    assert demo_source.settings.emissivity == 0.95
    assert demo_source.settings.reflectedC == 21.0


# --- StreamHub ---------------------------------------------------------------------


def test_hub_broadcast_and_remove():
    hub = StreamHub()
    q = hub.add()
    assert hub.client_count() == 1
    hub.broadcast(b"frame-1")
    assert q.get(timeout=1) == b"frame-1"
    hub.remove(q)
    assert hub.client_count() == 0
    assert q.get(timeout=1) is None  # sentinel wakes the sender thread


def test_hub_drops_newest_when_client_queue_full():
    hub = StreamHub()
    q = hub.add()  # maxsize=2
    hub.broadcast(b"a")
    hub.broadcast(b"b")
    hub.broadcast(b"c")  # dropped: queue full
    assert q.get_nowait() == b"a"
    assert q.get_nowait() == b"b"
    assert q.empty()


# --- DemoSource / stream_worker ------------------------------------------------------


def test_demo_source_read_shape():
    temps = DemoSource().read()
    assert len(temps) == COLS * ROWS
    assert all(math.isfinite(v) for v in temps)


class FailingSource(DemoSource):
    name = "failing"

    def read(self) -> list[float]:
        raise RuntimeError("i2c timeout")


def _run_worker_briefly(src, seconds=0.35, fps=100.0):
    stop = threading.Event()
    t = threading.Thread(target=stream_worker, args=(src, stop, fps), daemon=True)
    t.start()
    time.sleep(seconds)
    stop.set()
    t.join(timeout=5)


def test_stream_worker_broadcasts_valid_frames():
    q = main.hub.add()
    stop = threading.Event()
    worker = threading.Thread(
        target=stream_worker, args=(DemoSource(), stop, 100.0), daemon=True
    )
    before = main.stats.seq
    worker.start()
    frames = []
    try:
        # consume while the worker runs (queue maxsize=2 drops the newest);
        # >1s so the worker's 1-second stats window ticks at least once
        deadline = time.time() + 1.2
        while time.time() < deadline:
            try:
                item = q.get(timeout=0.05)
            except queue.Empty:
                continue
            if item is not None:
                frames.append(item)
        assert len(frames) >= 5, "worker produced too few frames"
        seqs = []
        for data in frames:
            assert len(data) == FRAME_BYTES
            f = decode_frame(data)
            assert f["version"] == 1
            seqs.append(f["seq"])
        assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
        assert main.stats.seq > before
    finally:
        stop.set()
        worker.join(timeout=5)
        main.hub.remove(q)


def test_stream_worker_sends_fault_frame_on_sensor_error():
    q = main.hub.add()
    try:
        _run_worker_briefly(FailingSource(), seconds=0.25)
        data = q.get(timeout=2)
        assert decode_frame(data)["flags"] & FLAG_FAULT
    finally:
        main.hub.remove(q)


# --- HTTP routes ---------------------------------------------------------------------


@pytest.fixture
def client():
    main.app.config["TESTING"] = True
    return main.app.test_client()


def test_index_serves_ui(client):
    r = client.get("/")
    assert r.status_code == 200
    assert b"sourceLabel" in r.data and b"/ws/thermal" in r.data


def test_stream_contract_served(client):
    r = client.get("/stream-contract.html")
    assert r.status_code == 200
    assert b"Frame layout" in r.data


def test_api_status_without_source(client):
    r = client.get("/api/status")
    assert r.status_code == 200
    data = r.get_json()
    assert data["frameBytes"] is None  # no source attached
    assert data["source"] is None
    assert data["settings"] is None


def test_api_status_with_demo_source(client, monkeypatch):
    monkeypatch.setattr(main, "source", DemoSource())
    data = client.get("/api/status").get_json()
    assert data["source"] == "demo"
    assert data["demo"] is True
    assert data["frameBytes"] == FRAME_BYTES == 1568
    assert data["settings"] == {"emissivity": 0.95, "reflectedC": 21.0}
