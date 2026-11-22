import argparse
import io
import logging
import math
import signal
import sys
import threading
from datetime import datetime
from pathlib import Path

import adafruit_mlx90640
import board
import cv2
import numpy as np
from flask import Flask, Response, render_template, request, jsonify
from PIL import Image
from picamera2 import Picamera2
from picamera2.encoders import JpegEncoder
from picamera2.outputs import FileOutput

logger = logging.getLogger(__name__)

app = Flask(__name__)


MINTEMP = 0.0  # low range of the sensor (deg C)
MAXTEMP = 40.0  # high range of the sensor (deg C)
COLORDEPTH = 1000  # how many color values we can have
INTERPOLATE = 10  # scale factor for final image

IMAGE_CAM_WIDTH = 640
IMAGE_CAM_HEIGHT = 480

# Video recording settings
VIDEO_OUTPUT_DIR = "recordings"
VIDEO_FPS = 4  # Thermal camera refresh rate


class ThermalCamera:
    # Colormap presets: (position, (R, G, B)) where position is 0.0-1.0
    COLORMAP_PRESETS = {
        "rainbow": (
            (0.0, (0, 0, 0)),
            (0.20, (0, 0, 0.5)),
            (0.40, (0, 0.5, 0)),
            (0.60, (0.5, 0, 0)),
            (0.80, (0.75, 0.75, 0)),
            (0.90, (1.0, 0.75, 0)),
            (1.00, (1.0, 1.0, 1.0)),
        ),
        "ironbow": (
            (0.0, (0, 0, 0)),
            (0.15, (0, 0, 0.5)),
            (0.30, (0, 0.5, 1.0)),
            (0.50, (0, 1.0, 1.0)),
            (0.70, (1.0, 1.0, 0)),
            (0.85, (1.0, 0.5, 0)),
            (1.00, (1.0, 0, 0)),
        ),
        "grayscale": (
            (0.0, (0, 0, 0)),
            (1.0, (1.0, 1.0, 1.0)),
        ),
        "hot": (
            (0.0, (0, 0, 0)),
            (0.33, (1.0, 0, 0)),
            (0.66, (1.0, 1.0, 0)),
            (1.00, (1.0, 1.0, 1.0)),
        ),
        "cool": (
            (0.0, (0, 1.0, 1.0)),
            (1.0, (1.0, 0, 1.0)),
        ),
    }

    def __init__(self, refresh_rate_hz: float = 4.0):
        self.refresh_rates = {
            0.5: adafruit_mlx90640.RefreshRate.REFRESH_0_5_HZ,  # 0.5Hz
            1: adafruit_mlx90640.RefreshRate.REFRESH_1_HZ,  # 1Hz
            2: adafruit_mlx90640.RefreshRate.REFRESH_2_HZ,  # 2Hz
            4: adafruit_mlx90640.RefreshRate.REFRESH_4_HZ,  # 4Hz
            8: adafruit_mlx90640.RefreshRate.REFRESH_8_HZ,  # 8Hz
            16: adafruit_mlx90640.RefreshRate.REFRESH_16_HZ,  # 16Hz
            32: adafruit_mlx90640.RefreshRate.REFRESH_32_HZ,  # 32Hz
            64: adafruit_mlx90640.RefreshRate.REFRESH_64_HZ,  # 64Hz
        }

        self.width: int = 32
        self.height: int = 24
        self._target_refresh_rate_hz: float = refresh_rate_hz

        # Allocate reusable pixel buffer (frame buffer is allocated per-call now)
        self.pixels = [0] * self.width * self.height

        # Thread-safe colormap access
        self._colormap_lock = threading.Lock()
        self._current_colormap_name = "rainbow"
        self.colormap = self._get_colormap(
            self.COLORMAP_PRESETS[self._current_colormap_name]
        )

        self._connect()

    def _connect(self):
        self.mlx = adafruit_mlx90640.MLX90640(board.I2C())
        self.refresh_rate = self._target_refresh_rate_hz

    @property
    def refresh_rate(self) -> float:
        return self._target_refresh_rate_hz

    @refresh_rate.setter
    def refresh_rate(self, value: float):
        """Set the refresh rate in hertz of the thermal camera to the nearest supported value."""
        # Find the nearest supported refresh rate
        nearest_rate = min(
            self.refresh_rates.keys(), key=lambda rate: abs(rate - value)
        )
        enum_value = self.refresh_rates[nearest_rate]

        if abs(nearest_rate - value) > 0.1:
            logger.warning(
                f"Requested refresh rate {value} Hz not available, "
                f"using nearest supported rate: {nearest_rate} Hz"
            )
        else:
            logger.info(f"Setting refresh rate to {nearest_rate} Hz")

        self._target_refresh_rate_hz = nearest_rate
        self.mlx.refresh_rate = enum_value

    def _get_colormap(self, heatmap):
        """Generate colormap from heatmap definition."""
        colormap = [0] * COLORDEPTH

        for i in range(COLORDEPTH):
            colormap[i] = gradient(i, COLORDEPTH, heatmap)

        return colormap

    def set_colormap(self, name: str) -> bool:
        """Set the colormap by name. Returns True if successful, False otherwise."""
        if name not in self.COLORMAP_PRESETS:
            logger.warning(f"Unknown colormap: {name}")
            return False

        with self._colormap_lock:
            self._current_colormap_name = name
            self.colormap = self._get_colormap(self.COLORMAP_PRESETS[name])
            logger.info(f"Colormap changed to: {name}")

        return True

    def get_colormap_name(self) -> str:
        """Get the current colormap name."""
        return self._current_colormap_name

    def get_frame(self) -> list[float]:
        """Get a fresh temperature frame from the sensor.

        Returns:
            list[float]: A new list containing the current temperature values
        """
        frame = [0] * self.width * self.height
        self.mlx.getFrame(frame)
        return frame

    def get_image_and_frame(self) -> tuple[Image.Image, list[float]]:
        """Get both the thermal image and the raw temperature frame in a single call.

        Returns:
            tuple: (image, frame) where image is the colored thermal image and
                   frame is the raw temperature values
        """
        # Get a fresh frame (get_frame now returns a new list each time)
        frame = self.get_frame()
        pixels = self.pixels

        # Thread-safe colormap access
        with self._colormap_lock:
            colormap = self.colormap

        for i, pixel in enumerate(frame):
            coloridx = map_value(pixel, MINTEMP, MAXTEMP, 0, COLORDEPTH - 1)
            coloridx = int(constrain(coloridx, 0, COLORDEPTH - 1))
            pixels[i] = colormap[coloridx]

        # Save to JPEG image
        img = Image.new("RGB", (self.width, self.height))
        img.putdata(pixels)
        img = img.transpose(Image.Transpose.FLIP_TOP_BOTTOM)

        # Return the image and frame (frame is already a new list from get_frame)
        return img, frame


class StreamingOutput(io.BufferedIOBase):
    def __init__(self):
        self.frame = None
        self.condition = threading.Condition()

    def write(self, buf):
        with self.condition:
            self.frame = buf
            self.condition.notify_all()


image_camera_output = StreamingOutput()
thermal_camera_output = StreamingOutput()

# Shared thermal camera instance (created in main())
thermal_camera_instance = None
thermal_camera_lock = threading.Lock()


# some utility functions
def constrain(val, min_val, max_val):
    return min(max_val, max(min_val, val))


def map_value(x, in_min, in_max, out_min, out_max):
    return (x - in_min) * (out_max - out_min) / (in_max - in_min) + out_min


def gaussian(x, a, b, c, d=0):
    return a * math.exp(-((x - b) ** 2) / (2 * c**2)) + d


def gradient(x, width, cmap, spread=1):
    width = float(width)
    r = sum(
        gaussian(x, p[1][0], p[0] * width, width / (spread * len(cmap))) for p in cmap
    )
    g = sum(
        gaussian(x, p[1][1], p[0] * width, width / (spread * len(cmap))) for p in cmap
    )
    b = sum(
        gaussian(x, p[1][2], p[0] * width, width / (spread * len(cmap))) for p in cmap
    )
    r = int(constrain(r * 255, 0, 255))
    g = int(constrain(g * 255, 0, 255))
    b = int(constrain(b * 255, 0, 255))
    return r, g, b


def init_image_camera():
    """Initialize the image camera. Returns True if successful, False otherwise."""
    try:
        picam2 = Picamera2()
        picam2.configure(
            picam2.create_video_configuration(
                main={"size": (IMAGE_CAM_WIDTH, IMAGE_CAM_HEIGHT)}
            )
        )
        picam2.start_recording(JpegEncoder(), FileOutput(image_camera_output))
        logger.info("Image camera initialized successfully")
        return True
    except (IndexError, RuntimeError, Exception) as e:
        logger.warning(f"Failed to initialize image camera: {e}")
        logger.info("Continuing without image camera - thermal camera will still work")
        return False


def thermal_camera_worker(
    output: io.IOBase,
    stop_event: threading.Event,
    thermal_camera: ThermalCamera,
    min_temp_threshold: float = None,
    max_temp_threshold: float = None,
):

    # Setup video recording
    video_output_path = Path(VIDEO_OUTPUT_DIR)
    video_output_path.mkdir(parents=True, exist_ok=True)
    video_filename = (
        video_output_path
        / f"{datetime.now().strftime('%Y-%m-%dT%H-%M-%S')}_thermal.mp4"
    )
    video_width = thermal_camera.width * INTERPOLATE
    video_height = thermal_camera.height * INTERPOLATE

    # Get the actual refresh rate being used for video FPS
    actual_refresh_rate = thermal_camera.refresh_rate

    # Try H.264 codec first (more compatible), fallback to mp4v
    fourcc = cv2.VideoWriter_fourcc(*"avc1")
    video_writer = cv2.VideoWriter(
        str(video_filename), fourcc, actual_refresh_rate, (video_width, video_height)
    )

    # If H.264 fails, try mp4v
    if not video_writer.isOpened():
        logger.warning("H.264 codec not available, trying mp4v")
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        video_writer = cv2.VideoWriter(
            str(video_filename),
            fourcc,
            actual_refresh_rate,
            (video_width, video_height),
        )

    if not video_writer.isOpened():
        logger.error(f"Failed to open video writer for {video_filename}")
        logger.error(
            f"Video writer error details - fourcc: {fourcc}, fps: {actual_refresh_rate}, resolution: ({video_width}, {video_height})"
        )
        video_writer = None
    else:
        logger.info(f"Recording thermal camera to {video_filename}")
        logger.info(
            f"Video settings - codec: {chr(fourcc & 0xFF) + chr((fourcc >> 8) & 0xFF) + chr((fourcc >> 16) & 0xFF) + chr((fourcc >> 24) & 0xFF)}, fps: {actual_refresh_rate}, resolution: {video_width}x{video_height}"
        )

    try:
        while not stop_event.is_set():
            # Get image and frame from thermal camera
            success = False
            img = None
            temp_frame = None
            while not success and not stop_event.is_set():
                try:
                    img, temp_frame = thermal_camera.get_image_and_frame()
                    success = True
                except ValueError:
                    continue
                except RuntimeError as e:
                    logger.warning("Unable to get frame", exc_info=e)
                    continue

            if stop_event.is_set():
                break

            # Resize image for display/video
            img_resized = img.resize(
                (video_width, video_height),
                Image.Resampling.BICUBIC,
            )

            # Check if we should write this frame based on temperature thresholds
            should_write_frame = True
            if min_temp_threshold is not None or max_temp_threshold is not None:
                should_write_frame = False

                # Check if any pixel meets the temperature criteria
                for temp in temp_frame:
                    if (min_temp_threshold is None or temp >= min_temp_threshold) and (
                        max_temp_threshold is None or temp <= max_temp_threshold
                    ):
                        should_write_frame = True
                        break

            # Write frame to video file if conditions are met
            if video_writer and video_writer.isOpened() and should_write_frame:
                try:
                    # Convert PIL Image to OpenCV format (RGB to BGR)
                    img_array = np.array(img_resized)
                    img_bgr = cv2.cvtColor(img_array, cv2.COLOR_RGB2BGR)
                    video_writer.write(img_bgr)

                except Exception as e:
                    logger.error(f"Failed to write frame to video: {e}")
                    # Attempt to recover by reopening the video writer
                    try:
                        if video_writer.isOpened():
                            video_writer.release()
                        video_writer = cv2.VideoWriter(
                            str(video_filename),
                            fourcc,
                            actual_refresh_rate,
                            (video_width, video_height),
                        )
                        if video_writer.isOpened():
                            logger.info("Successfully recovered video writer")
                        else:
                            logger.error(
                                "Failed to recover video writer, disabling video recording"
                            )
                            video_writer = None
                    except Exception as recovery_error:
                        logger.error(
                            f"Failed to recover video writer: {recovery_error}"
                        )
                        video_writer = None

            # Write to streaming output
            buf = io.BytesIO()
            img_resized.save(buf, format="jpeg")
            output.write(buf.getvalue())
    finally:
        # Release video writer when done
        if video_writer and video_writer.isOpened():
            video_writer.release()
            logger.info(f"Video recording saved to {video_filename}")


@app.route("/")
def index():
    """Video streaming home page."""
    return render_template("index.html")


# def gen(q: queue.Queue):
#     """Video streaming generator function."""
#     while True:
#         frame = q.get()
#         q.task_done()
#         logger.debug("Received frame")
#         yield (b"--frame\r\n" b"Content-Type: image/jpeg\r\n\r\n" + frame + b"\r\n")


def gen_from_stream(output: StreamingOutput):
    """Video streaming generator function."""
    while True:
        with output.condition:
            output.condition.wait()
            frame = output.frame
        logger.debug("Received frame")
        yield (b"--frame\r\n" b"Content-Type: image/jpeg\r\n\r\n" + frame + b"\r\n")


@app.route("/video_feed")
def video_feed():
    """Video streaming route. Put this in the src attribute of an img tag."""
    return Response(
        gen_from_stream(thermal_camera_output),
        mimetype="multipart/x-mixed-replace; boundary=frame",
    )


@app.route("/image_camera_feed.mjpg")
def image_camera_feed():
    """Video streaming route. Put this in the src attribute of an img tag."""
    return Response(
        gen_from_stream(image_camera_output),
        mimetype="multipart/x-mixed-replace; boundary=frame",
    )


@app.route("/api/colormap", methods=["POST"])
def set_colormap():
    """API endpoint to change the thermal camera colormap."""
    global thermal_camera_instance

    if thermal_camera_instance is None:
        return jsonify({"error": "Thermal camera not initialized"}), 503

    data = request.get_json()
    if not data or "colormap" not in data:
        return jsonify({"error": "Missing 'colormap' parameter"}), 400

    colormap_name = data["colormap"]
    if colormap_name not in ThermalCamera.COLORMAP_PRESETS:
        return (
            jsonify(
                {
                    "error": f"Invalid colormap. Available: {list(ThermalCamera.COLORMAP_PRESETS.keys())}"
                }
            ),
            400,
        )

    with thermal_camera_lock:
        success = thermal_camera_instance.set_colormap(colormap_name)

    if success:
        return jsonify({"success": True, "colormap": colormap_name}), 200
    else:
        return jsonify({"error": "Failed to set colormap"}), 500


@app.route("/api/colormap", methods=["GET"])
def get_colormap():
    """API endpoint to get the current thermal camera colormap."""
    global thermal_camera_instance

    if thermal_camera_instance is None:
        return jsonify({"error": "Thermal camera not initialized"}), 503

    with thermal_camera_lock:
        current_colormap = thermal_camera_instance.get_colormap_name()
        available_colormaps = list(ThermalCamera.COLORMAP_PRESETS.keys())

    return (
        jsonify(
            {
                "current": current_colormap,
                "available": available_colormaps,
            }
        ),
        200,
    )


def main():
    # Parse command line arguments
    parser = argparse.ArgumentParser(description="Thermal Camera Streaming Server")
    parser.add_argument(
        "--min-temp",
        type=float,
        default=None,
        help="Minimum temperature threshold for recording frames (degrees C)",
    )
    parser.add_argument(
        "--max-temp",
        type=float,
        default=None,
        help="Maximum temperature threshold for recording frames (degrees C)",
    )
    args = parser.parse_args()

    # Enhanced logging configuration for both console and file output
    log_handlers = [logging.StreamHandler(sys.stdout)]

    # Add file handler for persistent logging
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

    # Log startup information
    logger.info(f"Starting TemperatureCamera server")
    logger.info(f"Python version: {sys.version}")
    logger.info(f"Working directory: {Path.cwd()}")
    if args.min_temp is not None or args.max_temp is not None:
        threshold_info = []
        if args.min_temp is not None:
            threshold_info.append(f"min {args.min_temp}°C")
        if args.max_temp is not None:
            threshold_info.append(f"max {args.max_temp}°C")
        logger.info(f"Temperature filtering enabled: {', '.join(threshold_info)}")
    else:
        logger.info("Recording all frames (no temperature filtering)")

    # Create stop event for graceful thread shutdown
    stop_event = threading.Event()

    def signal_handler(signum, frame):
        """Handle shutdown signals gracefully."""
        logger.info("Received shutdown signal, stopping threads...")
        stop_event.set()
        # Re-raise KeyboardInterrupt to stop Flask's app.run()
        raise KeyboardInterrupt

    # Register signal handlers for graceful shutdown
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    if not init_image_camera():
        logger.error("Failed to initialize image camera, continuing without it")
    else:
        logger.info("Image camera initialized successfully")

    # Create shared thermal camera instance
    global thermal_camera_instance
    with thermal_camera_lock:
        thermal_camera_instance = ThermalCamera(refresh_rate_hz=VIDEO_FPS)

    # Log temperature threshold settings
    if args.min_temp is not None or args.max_temp is not None:
        threshold_info = []
        if args.min_temp is not None:
            threshold_info.append(f"min {args.min_temp}°C")
        if args.max_temp is not None:
            threshold_info.append(f"max {args.max_temp}°C")
        logger.info(f"Temperature filtering enabled: {', '.join(threshold_info)}")
    else:
        logger.info("Recording all frames (no temperature filtering)")

    # Start thermal camera worker thread (non-daemon so it can clean up)
    thermal_thread = threading.Thread(
        target=thermal_camera_worker,
        args=(
            thermal_camera_output,
            stop_event,
            thermal_camera_instance,
            args.min_temp,
            args.max_temp,
        ),
    )
    thermal_thread.start()

    try:
        app.run(host="0.0.0.0", threaded=True)
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received")
    finally:
        # Signal the thread to stop
        stop_event.set()
        # Wait for thread to finish gracefully
        thermal_thread.join(timeout=30.0)


if __name__ == "__main__":
    main()
