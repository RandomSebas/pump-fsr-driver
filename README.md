# Pump FSR Driver

Desktop mini-driver + **Arduino** firmware for a **Pump It Up**-style dance pad
with 5 **FSR** sensors.

- **Python app** (`pump_driver.py`): CustomTkinter GUI that streams the 5 FSR
  sensors in real time (live vertical bars + a pad-style input viewer), lets you
  adjust each panel's activation threshold live (sent over serial, no
  recompiling), and persists the config to a local JSON file.
- **Arduino firmware** (`FSR_PUMP_warrior.ino`): reads the 5 FSRs, emulates a
  USB keyboard (`w a s d x`), drives 5 LEDs, and streams over serial at
  115200 baud.

## Hardware requirements

- **Arduino Leonardo / Micro / Pro Micro** (native USB; uses `Keyboard.h`.
  A regular UNO will NOT work).
- 5 FSR sensors + resistors (voltage divider) on `A0-A4`.
- 5 LEDs (with series resistors) on `3-7`.

Sensor index mapping (same in firmware and app):

| Index | Panel           | Key  | Pin  | LED |
|-------|-----------------|------|------|-----|
| 0     | Center          | w    | A0   | 3   |
| 1     | Down-left       | a    | A1   | 4   |
| 2     | Up-left         | s    | A2   | 5   |
| 3     | Up-right        | d    | A3   | 6   |
| 4     | Down-right      | x    | A4   | 7   |

## Installation & usage (script mode)

```bash
pip install -r requirements.txt
python pump_driver.py
```

1. Upload `FSR_PUMP_warrior/FSR_PUMP_warrior.ino` with the Arduino IDE
   (board **Leonardo**).
2. In the app select the serial port (listed as `COMx - Arduino Leonardo` on
   Windows, or `/dev/ttyACM0 - Arduino Leonardo` on Linux), press **Connect**,
   and adjust thresholds with the sliders or number fields.

## Linux setup

The firmware, protocol, and app are identical — only access to the serial port
differs.

1. **Port name**: on Linux the Leonardo shows up as `/dev/ttyACM0` (or
   `/dev/ttyACM1`, ...) instead of a COM port. The app lists it automatically;
   run `python test_serial.py` or `ls /dev/ttyACM*` to confirm.
2. **Permissions**: your user needs read/write on the port. Either add yourself
   to the serial group of your distro —
   `sudo usermod -aG dialout $USER` (Debian/Ubuntu), `uucp` (Arch), `lock`
   (Fedora) — then log out and back in, **or** install the provided udev rule
   (this uses systemd `uaccess`, so the user logged in at the console gets
   access automatically — no groups, no logout):

   ```bash
   sudo cp udev/99-pump-fsr.rules /etc/udev/rules.d/
   sudo udevadm control --reload-rules
   sudo udevadm trigger
   # then unplug/replug the USB
   ```

   The rule also sets `ID_MM_DEVICE_IGNORE` so **ModemManager** won't grab the
   board.
3. **Port vanishing / appearing and disappearing** (Ubuntu 22.04+): the
   `brltty` braille daemon claims `/dev/ttyACM0`. Remove it with
   `sudo apt remove brltty`.
4. Select the port in the app and press **Connect**. Same four-step: DTR is
   asserted, it waits for the board to reset, then handshakes with
   `PING`/`GETALL`.

## Serial protocol (115200 baud)

- Arduino → PC: `FSR:v1,v2,v3,v4,v5\n` (every 30 ms), `THRESH:i=v\n`,
  `READY:...\n` (boot banner), `PONG`, `ERR:...`.
- PC → Arduino: `SET,i,v\n` (v 0-1023), `GET,i\n`, `GETALL\n`, `PING\n`.

## Build to .exe (Windows)

```bash
build_exe.bat
```

or manually:

```bash
python -m PyInstaller --noconfirm --onefile --windowed --name PumpFSRDriver --collect-all customtkinter pump_driver.py
```

## Build to binary (Linux)

```bash
./build_linux.sh
```

(produces `dist/PumpFSRDriver`; the threshold config is saved next to it).

## Troubleshooting

If the board is listed but the app says "no response":

```bash
python test_serial.py COM3 115200     # Windows: use your actual COM port
python test_serial.py /dev/ttyACM0 115200   # Linux
```

- The Leonardo **must be opened with DTR asserted** (like the IDE Serial
  Monitor); with DTR low this board does not transmit.
- If no `FSR:` lines arrive: close the IDE Serial Monitor, check the USB cable
  (data, not charge-only), or re-upload the firmware.
- On Linux, if the port cannot be opened, check the `dialout`/`uucp` group and
  the udev rule above; if the port keeps disappearing, remove `brltty`.
- `test_serial.py` opens the port raw (like the IDE) to isolate hardware vs.
  app problems.

## Credits & Attribution

This project is a work based on the original work of **KIOPads**. The original
Arduino firmware (`FSR_PUMP_warrior.ino`) is entirely the work of KIOPads and
is included here with full attribution and credit. This repository contributes
the desktop driver app, the serial protocol, and the adaptations built around
that original code.

## License

MIT — see [LICENSE](LICENSE).