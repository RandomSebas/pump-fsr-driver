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
2. In the app select the COM port (listed as "COMx - Arduino Leonardo"),
   press **Connect**, and adjust thresholds with the sliders or number fields.

## Serial protocol (115200 baud)

- Arduino → PC: `FSR:v1,v2,v3,v4,v5\n` (every 30 ms), `THRESH:i=v\n`,
  `READY:...\n` (boot banner), `PONG`, `ERR:...`.
- PC → Arduino: `SET,i,v\n` (v 0-1023), `GET,i\n`, `GETALL\n`, `PING\n`.

## Build to .exe

```bash
build_exe.bat
```

or manually:

```bash
python -m PyInstaller --noconfirm --onefile --windowed --name PumpFSRDriver --collect-all customtkinter pump_driver.py
```

## Troubleshooting

If the board is listed but the app says "no response":

```bash
python test_serial.py COM3 115200     # use your actual COM port
```

- The Leonardo **must be opened with DTR asserted** (like the IDE Serial
  Monitor); with DTR low this board does not transmit.
- If no `FSR:` lines arrive: close the IDE Serial Monitor, check the USB cable
  (data, not charge-only), or re-upload the firmware.
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