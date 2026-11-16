"""Manage the headless LibreHardwareMonitorLib helper on Windows."""

import atexit
import json
import logging
import queue
import subprocess
import threading
from pathlib import Path


LOGGER = logging.getLogger("node")
HELPER_PATH = Path(__file__).parent / "windows-sensors" / "publish" / "WindowsSensors.exe"
TEMPERATURE_FIELDS = ("cpu_temperature_c", "gpu_temperature_c")


class WindowsSensors:
    def __init__(self, helper_path=HELPER_PATH, timeout=10):
        self.helper_path = helper_path
        self.timeout = timeout
        self.process = None
        self.responses = None
        self.reader = None
        self.lock = threading.RLock()
        self.last_warning = None

    def _start(self):
        self.process = subprocess.Popen(
            [str(self.helper_path)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            text=True, encoding="utf-8", creationflags=subprocess.CREATE_NO_WINDOW,
        )
        self.responses = queue.Queue()
        self.reader = threading.Thread(
            target=self._read_output,
            args=(self.process.stdout, self.responses),
            daemon=True,
        )
        self.reader.start()

    @staticmethod
    def _read_output(stdout, responses):
        try:
            for line in stdout:
                responses.put(line)
        finally:
            responses.put(None)

    def read(self):
        with self.lock:
            try:
                if self.process is None:
                    self._start()
                self.process.stdin.write("sample\n")
                self.process.stdin.flush()
                line = self.responses.get(timeout=self.timeout)
                if line is None:
                    raise OSError("Windows temperature helper exited without a reading")
                result = json.loads(line)
                if not isinstance(result, dict):
                    raise ValueError("Windows temperature helper returned an invalid response")
                if "error" in result:
                    raise OSError("Windows temperature helper: {}".format(result["error"]))
                for field in TEMPERATURE_FIELDS:
                    if field not in result:
                        raise ValueError("Windows temperature helper omitted {}".format(field))
                    value = result[field]
                    if value is not None and (
                        isinstance(value, bool) or not isinstance(value, (float, int))
                        or not 0 <= value <= 1000
                    ):
                        raise ValueError("Windows temperature helper returned invalid {}".format(field))
            except (OSError, ValueError, queue.Empty) as error:
                self.close()
                message = (
                    "Windows temperatures unavailable: {}. Publish windows-sensors first; "
                    "some sensors require running the node as administrator."
                ).format("helper timed out" if isinstance(error, queue.Empty) else error)
                self._warn(message)
                return {field: None for field in TEMPERATURE_FIELDS}

            missing = [field for field in TEMPERATURE_FIELDS if result[field] is None]
            self._warn(
                "LibreHardwareMonitorLib could not read {}. Hardware may not expose these "
                "sensors, or the node may need administrator privileges.".format(", ".join(missing))
                if missing else None
            )
            return {field: result[field] for field in TEMPERATURE_FIELDS}

    def _warn(self, message):
        if message and message != self.last_warning:
            LOGGER.warning(message)
        self.last_warning = message

    def close(self):
        with self.lock:
            if self.process is None:
                return
            process, reader = self.process, self.reader
            try:
                try:
                    process.stdin.close()
                except BrokenPipeError:
                    LOGGER.debug("Windows temperature helper closed its input before shutdown")
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
                reader.join(timeout=2)
            finally:
                process.stdout.close()
                self.process = None
                self.reader = None
                self.responses = None


WINDOWS_SENSORS = WindowsSensors()
atexit.register(WINDOWS_SENSORS.close)
