# Pico W configuration — edit on the device after copying (see README.md)
WIFI_SSID = "YOUR_WIFI_SSID"        # Pico W is 2.4 GHz only
WIFI_PASSWORD = "YOUR_WIFI_PASSWORD"

I2C_FREQ = 1000000                  # MLX90640 supports up to 1 MHz; use 400000 if reads fail
STREAM_FPS = 8                      # target stream rate (RP2040 realistically does 4-10 fps)
SENSOR_REFRESH_HZ = 8               # MLX90640 measurement rate: 0.5, 1, 2, 4, 8, 16, 32, 64
PORT = 80

FLIP_H = False                      # set True if the image is mirrored left-right
FLIP_V = False                      # set True if the image is upside down
