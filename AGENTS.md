# AGENTS.md

Python desktop driver + Arduino firmware for a Pump It Up dance pad with 5 FSR
sensors. GUI (`pump_driver.py`) streams FSR values over serial and lets the user
adjust each sensor's activation threshold live; the Arduino emulates a keyboard
(`w a s d x`) and drives 5 LEDs.

## Files

- `FSR_PUMP_warrior/FSR_PUMP_warrior.ino` — Arduino firmware (Keyboard.h HID).
- `pump_driver.py` — CustomTkinter desktop app + serial reader thread.
- `requirements.txt` — customtkinter, pyserial, pyinstaller.
- `build_exe.bat` — builds `dist/PumpFSRDriver.exe` (PyInstaller onefile).

## Serial protocol (115200 baud, both sides)

- Arduino -> PC: `FSR:v1,v2,v3,v4,v5\n` every 30 ms; `THRESH:i=v\n`; `READY:...\n` (boot banner); `PONG`; `ERR:...`.
- PC -> Arduino: `SET,i,v\n` (v 0-1023), `GET,i\n`, `GETALL\n`, `PING\n`.

## Sensor index mapping (CRITICAL — must match firmware and app)

`0`=CENTRO key `w` A0, `1`=DL key `a` A1, `2`=UL key `s` A2, `3`=UR key `d` A3,
`4`=DR key `x` A4. Pins A0-A4, LEDs on 3-7, thresholds default `[120,550,200,200,450]`.

## Gotchas

- Board MUST have native USB HID (Leonardo/Micro/Pro Micro). An Uno cannot use
  `Keyboard.h` — this will not work on Uno.
- Leonardo quirk: it MUST be opened with DTR asserted (like the IDE monitor).
  With DTR low this board does not transmit at all (Python sees nothing while
  the IDE works). The app opens `dtr=True`, waits 2 s for the reset+re-enum,
  clears the input buffer, and retries `PING`/`GETALL` until it sees
  `READY:`/`FSR:`/`THRESH:`. If the handle dies, it auto-reopens once.
- If the app connects but shows "sin respuesta": the board is likely running the
  OLD 9600-baud firmware — re-flash `FSR_PUMP_warrior.ino`, or the Serial Monitor
  still holds the port, or the USB cable is charge-only.
- CRITICAL: the serial reader thread must NEVER touch Tk widgets (crashes the
  thread silently → "connected but no data"). It only sets flags and fills
  `status_queue`; the GUI polls those in `_poll`. `_mark_confirmed` is reached
  from the reader thread and must stay widget-free.
- `test_serial.py` opens the COM raw (like the IDE monitor) to isolate hardware
  vs. app problems (RX shows every byte; typing `PING`/`GETALL`/`SET,0,300`).
- The original firmware had `for (int i = 0; i < 6; i++)` over 5-element arrays
  (out-of-bounds). All loops must stay `i < 5` (`SENSOR_COUNT`).
- FSR values are `analogRead` 0-1023; the app clamps to this range.
- `SET` from the app is debounced (200 ms) so rapid slider drags don't flood serial.
- Config JSON (`pump_driver_config.json`) is stored next to the script/exe.

## Commands

- Run app: `python pump_driver.py`
- Build exe: `build_exe.bat` or
  `python -m PyInstaller --noconfirm --onefile --windowed --name PumpFSRDriver --collect-all customtkinter pump_driver.py`
- Test without hardware: `python -c "import pump_driver; a=pump_driver.FSRDriverApp(); a.update(); a._handle_line('FSR:100,200,300,400,500'); a.update(); print(a.values); a._on_close()"`
