# Temperature Camera Streaming Server

A Flask-based thermal camera server. The Raspberry Pi sends raw MLX90640 temperature
frames to the browser over a binary WebSocket (stream contract v1); the browser does all
rendering and recording.

## Features

- Real-time thermal frames as v1 binary WebSocket messages (1,568 bytes/frame, 12 fps default)
- Client-side palettes (iron, white hot, black hot, rainbow, arctic, lava), auto/manual
  temperature range, °C/°F, hot/cold spot markers
- Browser-side snapshots (PNG) and session clips (WebM via MediaRecorder)
- Stream contract page served at `/stream-contract.html`
- Demo mode (`--demo`) streams a synthetic field without hardware
- Systemd service for production deployment

## Hardware Requirements

- Any Raspberry Pi with I2C and a network connection — Pi Zero 2 W, 2, 3, 4 or 5
  (the original Pi Zero also works headless; Wi-Fi needs a USB dongle)
- MLX90640 Thermal Camera Sensor

## Software Requirements

- Python 3.9+ (the floor is set by Flask 3.1; Raspberry Pi OS Bookworm ships 3.11)
- Raspberry Pi OS, 32-bit or 64-bit — the server only streams ~19 kB/s, either works
- I2C enabled

## Installation

### 1. System Setup

```bash
# Update system
sudo apt-get update && sudo apt-get upgrade -y

# Install dependencies
sudo apt-get install -y python3-pip python3-venv

# Enable I2C
sudo raspi-config
# Navigate to: Interface Options -> I2C -> Enable

# Increase I2C baud rate for better performance (optional)
echo "dtparam=i2c_arm_baudrate=1000000" | sudo tee -a /boot/config.txt

# Reboot
sudo reboot
```

### 2. Python Dependencies

```bash
# Install Python packages (on the Pi, also install the sensor library)
pip3 install -r requirements.txt
pip3 install -r requirements-pi.txt   # Raspberry Pi only
```

### 3. Deployment Options

#### Option A: Development Mode

```bash
python3 main.py
# Access at http://localhost:5000
```

#### Option B: Demo Mode (no hardware needed)

```bash
python3 main.py --demo
# Streams a synthetic thermal field; the UI also falls back to it
# automatically whenever the socket is unreachable
```

#### Option C: Production Deployment (Recommended)

```bash
# Install as systemd service
make install-service

# Service management commands
make start      # Start the service
make stop       # Stop the service
make restart    # Restart the service
make status     # Check service status
make logs       # View logs
make update     # Update to latest version
```

## Usage

### Web Interface

- **Live view**: `http://<raspberry-pi-ip>:5000/`
- **Stream contract**: `http://<raspberry-pi-ip>:5000/stream-contract.html`

### API Endpoints

#### Server Status
```bash
GET /api/status
# => {"source": "mlx90640"|"demo", "clients": N, "seq": ..., "fps": ..., "fault": ..., "settings": {...}}
```

#### Thermal Frame Stream (WebSocket)

`ws://<raspberry-pi-ip>:5000/ws/thermal` — one binary v1 frame per message
(32-byte header + 32×24 int16 hundredths-°C, see the stream contract). Text
messages are the control channel: `{"type":"ping","id":7}` → `{"type":"pong","id":7}`,
and `{"type":"settings","emissivity":0.95,"reflectedC":21.0}` to update the
physical model on the Pi.

## Configuration

Command-line options for `main.py`:

- `--demo`: stream a synthetic thermal field instead of the sensor
- `--host` / `--port`: bind address (default `0.0.0.0:5000`)
- `--fps`: stream rate in frames per second (default: 12)

Stream constants (protocol version, header layout, 32×24 grid, 1,568-byte
frames) live at the top of `main.py` and mirror `/stream-contract.html`.

## Development

```bash
pip install -r requirements-dev.txt
make test                  # unit tests (no hardware needed)
python3 main.py --demo     # run the server without a sensor
```

## File Structure

- `main.py`: Flask server, v1 frame encoder, WebSocket hub, sensor/demo sources
- `templates/index.html`: Live view UI (canvas renderer, palettes, recording)
- `templates/stream-contract.html`: v1 stream contract served by the app
- `designs/`: Source design export the UI was built from
- `requirements.txt`: Python dependencies (any machine)
- `requirements-pi.txt`: Sensor library (Raspberry Pi only)
- `requirements-dev.txt`: Test runner (development machines)
- `tests/`: pytest unit tests
- `Makefile`: Deployment and venv setup
- `TemperatureCamera.service`: Systemd service configuration

## Troubleshooting

### I2C Issues
```bash
# Check if I2C device is detected
sudo i2cdetect -y 1

# If not detected, check connections and try:
sudo reboot
```

### Stream Issues
```bash
# Check server health and stream stats
curl http://localhost:5000/api/status

# Lower the stream rate if the network or client can't keep up
python3 main.py --fps 6
```

### Performance Issues
```bash
# Lower --fps if the client can't keep up (frames are dropped, never queued)
# Ensure proper cooling for Raspberry Pi
```

## License

MIT License - See LICENSE for details.

## Support

For issues and feature requests, please open a GitHub issue.

## Acknowledgements

- Adafruit for the MLX90640 library
- Raspberry Pi Foundation for the excellent hardware platform