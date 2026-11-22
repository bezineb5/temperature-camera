# Temperature Camera Streaming Server

A Flask-based thermal camera streaming server with temperature-based frame filtering.

## Features

- Real-time thermal camera streaming via MJPEG
- Temperature-based frame filtering (optional)
- Multiple colormap options (rainbow, ironbow, grayscale, hot, cool)
- ISO-format video recording with temperature thresholds
- REST API for colormap control
- Systemd service for production deployment

## Hardware Requirements

- Raspberry Pi (4 recommended)
- MLX90640 Thermal Camera Sensor
- Raspberry Pi Camera Module (optional, for visible light)

## Software Requirements

- Python 3.7+
- Raspberry Pi OS (64-bit recommended)
- I2C enabled
- Camera interface enabled

## Installation

### 1. System Setup

```bash
# Update system
sudo apt-get update && sudo apt-get upgrade -y

# Install dependencies
sudo apt-get install -y python3-pip python3-venv libatlas-base-dev libopenjp2-7 libtiff5

# Enable I2C and camera
sudo raspi-config
# Navigate to: Interface Options -> I2C -> Enable
# Navigate to: Interface Options -> Camera -> Enable

# Increase I2C baud rate for better performance (optional)
echo "dtparam=i2c_arm_baudrate=1000000" | sudo tee -a /boot/config.txt

# Reboot
sudo reboot
```

### 2. Python Dependencies

```bash
# Install Python packages
pip3 install -r requirements.txt
pip3 install --upgrade adafruit-circuitpython-mlx90640
```

### 3. Deployment Options

#### Option A: Development Mode

```bash
python3 main.py
# Access at http://localhost:5000
```

#### Option B: With Temperature Filtering

```bash
# Only record frames with pixels >= 25°C
python3 main.py --min-temp 25

# Only record frames with pixels <= 15°C  
python3 main.py --max-temp 15

# Only record frames with pixels between 20°C and 30°C
python3 main.py --min-temp 20 --max-temp 30
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

- **Main Stream**: `http://<raspberry-pi-ip>:5000/`
- **Thermal Feed**: `http://<raspberry-pi-ip>:5000/video_feed`
- **Visible Camera Feed**: `http://<raspberry-pi-ip>:5000/image_camera_feed.mjpg`

### API Endpoints

#### Get Current Colormap
```bash
GET /api/colormap
```

#### Set Colormap
```bash
POST /api/colormap
Content-Type: application/json

{
  "colormap": "rainbow"  # or "ironbow", "grayscale", "hot", "cool"
}
```

## Configuration

Edit the following constants in `main.py`:

- `MINTEMP`, `MAXTEMP`: Temperature range for colormap (default: 0.0°C to 40.0°C)
- `COLORDEPTH`: Number of color values (default: 1000)
- `INTERPOLATE`: Scale factor for final image (default: 10)
- `IMAGE_CAM_WIDTH`, `IMAGE_CAM_HEIGHT`: Visible camera resolution (default: 640x480)
- `VIDEO_OUTPUT_DIR`: Directory for video recordings (default: "recordings")
- `VIDEO_FPS`: Video recording frame rate (default: 4)

## File Structure

- `main.py`: Main application with Flask server and thermal camera logic
- `templates/index.html`: Web interface template
- `requirements.txt`: Python dependencies
- `Makefile`: Deployment and management scripts
- `TemperatureCamera.service`: Systemd service configuration
- `recordings/`: Directory for video recordings (created automatically)

## Troubleshooting

### I2C Issues
```bash
# Check if I2C device is detected
sudo i2cdetect -y 1

# If not detected, check connections and try:
sudo reboot
```

### Camera Issues
```bash
# Check camera status
vcgencmd get_camera

# Test camera
libcamera-hello
```

### Performance Issues
```bash
# Reduce resolution by modifying INTERPOLATE in main.py
# Lower VIDEO_FPS if frame rate is too high
# Ensure proper cooling for Raspberry Pi
```

## License

MIT License - See LICENSE for details.

## Support

For issues and feature requests, please open a GitHub issue.

## Acknowledgements

- Adafruit for the MLX90640 library
- Raspberry Pi Foundation for the excellent hardware platform
- OpenCV and PIL communities for image processing tools