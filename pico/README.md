# Pico W edition (standalone, no Raspberry Pi needed)

MicroPython port of the TemperatureCamera server. The Pico W reads the
MLX90640 over I2C, serves the web UI from its flash, and streams the same
v1 binary WebSocket frames as the Pi server — so `index.html` here is a
byte-identical copy of `templates/index.html` and needs no changes.

## Wiring (MLX90640 breakout → Pico W)

| MLX90640 | Pico W |
|---|---|
| VCC | 3V3 (pin 36) — **not** VSYS/5 V |
| GND | GND (pin 38) |
| SDA | GP4 (pin 6) |
| SCL | GP5 (pin 7) |

Add 2.2–4.7 kΩ pull-ups from SDA and SCL to 3V3 if your breakout doesn't
have them. Keep wires short (< 10 cm).

## Install

1. Flash CircuitPython for Pico W (drag the UF2 onto the BOOTSEL drive).
2. Install the sensor library and copy the files:

```bash
mpremote mip install adafruit-circuitpython-mlx90640
mpremote cp main.py config.py index.html stream-contract.html :
```
3. Edit `config.py` on the device (Wi-Fi credentials, I2C speed, fps, flips).
4. Reset the board (`mpremote run main.py` also works for a test run) —
   `main.py` in the flash root autostarts on boot.

Open `http://<pico-ip>/` — the UI connects to `ws://<pico-ip>/ws/thermal`
automatically. Check `/api/status` for `fps` / `fault`.

## Notes

- RP2040 realistically streams ~4–10 fps in MicroPython (I2C readout +
  encode). Lower `SENSOR_REFRESH_HZ`/`STREAM_FPS` if it stutters.
- I2C speed is wiring-dependent: try `I2C_FREQ = 1000000`, fall back to
  `400000` if you see `sensor read failed` in the REPL.
- If the image is mirrored, set `FLIP_H`/`FLIP_V` in `config.py`.
- Desktop testing without a Pico: the same `main.py` runs under CPython
  (demo sensor, Wi-Fi skipped) — `python3 main.py` with `PORT = 8080` in
  `config.py`.
