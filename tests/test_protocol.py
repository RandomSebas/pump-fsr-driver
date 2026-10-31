"""Protocol parser tests that do not need Tk or hardware.

`_handle_line` is called exactly as the serial reader calls it, but on a bare
FSRDriverApp instance (no window). This keeps the tests runnable headless.
"""
import queue
import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pump_driver  # noqa: E402


class FakeEntry:
    def __init__(self, text=""):
        self.text = text

    def get(self):
        return self.text


def make_app():
    app = object.__new__(pump_driver.FSRDriverApp)
    app.last_line = ""
    app.values = [0] * 5
    app.thresholds = list(pump_driver.DEFAULT_THRESHOLDS)
    app.data_lock = threading.Lock()
    app.status_queue = queue.Queue()
    app._confirmed = False
    app._handshake_failed = False
    app._request_sync = False
    app._last_fsr_monotonic = 0.0
    app._data_stale = True
    app._data_version = 0
    app._threshold_version = 0
    return app


def handle(app, line):
    pump_driver.FSRDriverApp._handle_line(app, line)


class ProtocolTests(unittest.TestCase):
    def test_fsr_line_parses_five_values(self):
        app = make_app()
        handle(app, "FSR:100,200,300,400,500")
        self.assertEqual(app.values, [100, 200, 300, 400, 500])
        self.assertEqual(app._data_version, 1)
        self.assertEqual(app.last_line, "FSR:100,200,300,400,500")

    def test_fsr_values_are_clamped(self):
        app = make_app()
        handle(app, "FSR:-5,0,1023,1024,99999")
        self.assertEqual(app.values, [0, 0, 1023, 1023, 1023])

    def test_fsr_missing_and_corrupt_values_become_zero(self):
        app = make_app()
        handle(app, "FSR:10,oops,30")
        self.assertEqual(app.values, [10, 0, 30, 0, 0])

    def test_fsr_extra_values_are_ignored(self):
        app = make_app()
        handle(app, "FSR:1,2,3,4,5,6,7")
        self.assertEqual(app.values, [1, 2, 3, 4, 5])

    def test_thresh_line_updates_one_sensor(self):
        app = make_app()
        handle(app, "THRESH:2=300")
        self.assertEqual(app.thresholds, [120, 550, 300, 200, 450])
        self.assertEqual(app._threshold_version, 1)

    def test_thresh_is_clamped_and_invalid_index_ignored(self):
        app = make_app()
        before = list(app.thresholds)
        handle(app, "THRESH:0=9999")
        handle(app, "THRESH:9=300")
        self.assertEqual(app.thresholds[0], 1023)
        self.assertEqual(app.thresholds[1:], before[1:])

    def test_corrupt_thresh_does_not_raise(self):
        app = make_app()
        before = list(app.thresholds)
        handle(app, "THRESH:2=abc")
        self.assertEqual(app.thresholds, before)

    def test_pong_and_ready_confirm_protocol(self):
        for line in ("PONG", "READY:FSR_PUMP v2 baud=115200"):
            app = make_app()
            handle(app, line)
            self.assertTrue(app._confirmed)
            self.assertTrue(app._request_sync)
            self.assertFalse(app._data_stale)

    def test_err_is_forwarded_to_log(self):
        app = make_app()
        handle(app, "ERR:comando_desconocido")
        self.assertIn("ERR:comando_desconocido", app.status_queue.get_nowait())

    def test_unknown_line_is_ignored(self):
        app = make_app()
        handle(app, "basura")
        self.assertEqual(app.values, [0] * 5)
        self.assertTrue(app.status_queue.empty())

    def test_entry_int_accepts_whitespace_and_rejects_text(self):
        self.assertEqual(
            pump_driver.FSRDriverApp._entry_int(FakeEntry(" 42 ")), 42)
        self.assertIsNone(
            pump_driver.FSRDriverApp._entry_int(FakeEntry("abc")))


if __name__ == "__main__":
    unittest.main()
