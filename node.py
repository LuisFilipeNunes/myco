"""Collect and exchange telemetry with other nodes."""

import argparse
import datetime
import json
import logging
import math
import os
import platform
import random
import re
import socket
import sqlite3
import subprocess
import threading
import time
import uuid
from contextlib import closing, contextmanager
from decimal import Decimal, localcontext
from pathlib import Path
from urllib import error, parse, request

import psutil
from flask import Flask, jsonify, render_template, request as flask_request

from windows_sensors import WINDOWS_SENSORS

LOGGER = logging.getLogger("node")
DEFAULT_DB_PATH = Path(__file__).with_name("telemetry.db")
METRIC_FIELDS = ("cpu_percent", "mem_percent", "disk_percent")
SYSTEM_INFO_FIELDS = (
    "cpu_count", "mem_used_bytes", "mem_total_bytes", "disk_used_bytes",
    "disk_total_bytes", "cpu_temperature_c", "gpu_temperature_c", "os_version",
)
SPORE_VERSION = 1
ONLINE_THRESHOLD_SECONDS = 15
STALE_THRESHOLD_SECONDS = 60
HISTORY_BUCKETS = 240
UTC_TIMESTAMP = re.compile(
    r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,324}))?Z$"
)


def positive_number(value):
    try:
        number = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive number") from exc
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be a positive finite number")
    return number


def peer_url(value):
    if not isinstance(value, str) or any(char.isspace() or ord(char) < 32 for char in value):
        raise ValueError("invalid peer URL")
    try:
        url = parse.urlsplit(value)
        hostname = url.hostname
        url.port
    except (ValueError, TypeError) as exc:
        raise ValueError("invalid peer URL") from exc
    if (
        url.scheme not in ("http", "https")
        or not hostname
        or url.username is not None
        or url.password is not None
        or url.query
        or url.fragment
    ):
        raise ValueError("peer URL must be an HTTP(S) URL without credentials, query, or fragment")
    return value.rstrip("/")


def cli_peer_url(value):
    try:
        return peer_url(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def finite_number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(float(value))
    except OverflowError:
        return False


def normalize_report(report):
    if not isinstance(report, dict):
        raise ValueError("each report must be a JSON object")
    hostname = report.get("hostname")
    device_type = report.get("device_type")
    timestamp = report.get("timestamp")
    metrics = report.get("metrics")
    if not isinstance(hostname, str) or not hostname.strip():
        raise ValueError("hostname must be a non-empty string")
    if not isinstance(device_type, str) or not device_type.strip():
        raise ValueError("device_type must be a non-empty string")
    if not finite_number(timestamp):
        raise ValueError("timestamp must be a finite number")
    try:
        datetime.datetime.fromtimestamp(math.floor(timestamp), datetime.timezone.utc)
    except (OverflowError, OSError, ValueError):
        raise ValueError("timestamp is outside the supported UTC date range")
    if not isinstance(metrics, dict):
        raise ValueError("metrics must be a JSON object")
    normalized = {}
    for field in METRIC_FIELDS:
        value = metrics.get(field)
        if not finite_number(value) or not 0 <= value <= 100:
            raise ValueError("metrics.{} must be a number between 0 and 100".format(field))
        normalized[field] = float(value)
    for field in SYSTEM_INFO_FIELDS:
        value = metrics.get(field)
        if field == "os_version":
            if value is not None and not isinstance(value, str):
                raise ValueError("metrics.os_version must be a string")
        elif value is not None:
            if not finite_number(value) or value < 0:
                raise ValueError("metrics.{} must be a non-negative number".format(field))
            if field == "cpu_count" and (not isinstance(value, int) or value == 0):
                raise ValueError("metrics.cpu_count must be a positive integer")
            normalized[field] = value if field == "cpu_count" else float(value)
        else:
            normalized[field] = None
        if field == "os_version":
            normalized[field] = value
    for used_field, total_field in (
        ("mem_used_bytes", "mem_total_bytes"),
        ("disk_used_bytes", "disk_total_bytes"),
    ):
        used, total = normalized[used_field], normalized[total_field]
        if used is not None and total is not None and used > total:
            raise ValueError("metrics.{} cannot exceed {}".format(used_field, total_field))
    custom = metrics.get("custom_metric", "")
    if not isinstance(custom, str) and not finite_number(custom):
        raise ValueError("metrics.custom_metric must be a string or finite number")
    normalized["custom_metric"] = str(custom)
    return {
        "hostname": hostname.strip(),
        "device_type": device_type.strip(),
        "timestamp": float(timestamp),
        "metrics": normalized,
    }


def report_to_spore(report):
    seconds = math.floor(report["timestamp"])
    timestamp = datetime.datetime.fromtimestamp(
        seconds, datetime.timezone.utc
    ).isoformat(timespec="seconds").replace("+00:00", "")
    with localcontext() as context:
        context.prec = 400
        fraction = Decimal(repr(report["timestamp"])) - seconds
        if fraction:
            timestamp += format(fraction, "f")[1:].rstrip("0")
    timestamp += "Z"
    return {
        "version": SPORE_VERSION,
        "spore_id": report["spore_id"],
        "source_id": report["hostname"],
        "type": "telemetry",
        "timestamp": timestamp,
        "payload": {"device_type": report["device_type"], **report["metrics"]},
    }


def normalize_message(message):
    if not isinstance(message, dict):
        raise ValueError("each message must be a JSON object")
    version = message.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version != SPORE_VERSION:
        raise ValueError("unsupported spore version")
    if message.get("type") != "telemetry":
        raise ValueError("unsupported spore type")
    spore_id = message.get("spore_id")
    source_id = message.get("source_id")
    timestamp = message.get("timestamp")
    payload = message.get("payload")
    if not isinstance(spore_id, str) or not spore_id.strip():
        raise ValueError("spore_id must be a non-empty string")
    if not isinstance(source_id, str) or not source_id.strip() or source_id != source_id.strip():
        raise ValueError("source_id must be a non-empty string without surrounding whitespace")
    match = UTC_TIMESTAMP.fullmatch(timestamp) if isinstance(timestamp, str) else None
    if not match:
        raise ValueError("timestamp must be a UTC timestamp ending in Z")
    try:
        seconds = int(datetime.datetime.fromisoformat(
            match.group(1) + "+00:00"
        ).timestamp())
        with localcontext() as context:
            context.prec = 400
            timestamp = float(Decimal(seconds) + Decimal("0." + (match.group(2) or "0")))
    except (OverflowError, OSError, ValueError) as exc:
        raise ValueError("timestamp must be a valid UTC date") from exc
    if not isinstance(payload, dict):
        raise ValueError("payload must be a JSON object")
    report = normalize_report({
        "hostname": source_id,
        "device_type": payload.get("device_type"),
        "timestamp": timestamp,
        "metrics": payload,
    })
    report["spore_id"] = spore_id
    return report


def report_status(timestamp, now=None):
    age = max(0, (time.time() if now is None else now) - timestamp)
    if age <= ONLINE_THRESHOLD_SECONDS:
        return "online"
    if age <= STALE_THRESHOLD_SECONDS:
        return "stale"
    return "offline"


class NodeState:
    def __init__(self, db_path, advertise_url, seed_nodes):
        self.db_path = str(db_path)
        self.advertise_url = peer_url(advertise_url)
        self.peers = {peer_url(url) for url in seed_nodes} - {self.advertise_url}
        self.peer_exchanges = {}
        self.reports = {}
        self.lock = threading.RLock()
        self._init_db()

    @contextmanager
    def connect_db(self):
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_db(self):
        with self.connect_db() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS telemetry_spores (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    hostname TEXT NOT NULL,
                    device_type TEXT NOT NULL,
                    cpu_percent REAL NOT NULL,
                    mem_percent REAL NOT NULL,
                    disk_percent REAL NOT NULL,
                    custom_metric TEXT NOT NULL DEFAULT '',
                    system_info_json TEXT NOT NULL DEFAULT '{}',
                    timestamp REAL NOT NULL,
                    spore_id TEXT NOT NULL
                )"""
            )
            columns = {
                row[1] for row in conn.execute("PRAGMA table_info(telemetry_spores)")
            }
            if "system_info_json" not in columns:
                conn.execute(
                    "ALTER TABLE telemetry_spores ADD COLUMN "
                    "system_info_json TEXT NOT NULL DEFAULT '{}'"
                )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_telemetry_spores_hostname_timestamp "
                "ON telemetry_spores (hostname, timestamp DESC, id DESC)"
            )
            rows = conn.execute(
                """SELECT m.hostname, m.device_type, m.cpu_percent, m.mem_percent,
                          m.disk_percent, m.custom_metric, m.system_info_json,
                          m.timestamp, m.spore_id
                   FROM telemetry_spores AS m
                   WHERE m.id = (
                       SELECT m2.id FROM telemetry_spores AS m2
                       WHERE m2.hostname = m.hostname
                       ORDER BY m2.timestamp DESC, m2.id DESC LIMIT 1
                   )"""
            ).fetchall()
        for hostname, device_type, cpu, memory, disk, custom, system_info, timestamp, spore_id in rows:
            metrics = {
                "cpu_percent": cpu, "mem_percent": memory,
                "disk_percent": disk, "custom_metric": custom,
            }
            metrics.update({field: None for field in SYSTEM_INFO_FIELDS})
            metrics.update(json.loads(system_info))
            report = {
                "hostname": hostname,
                "device_type": device_type,
                "timestamp": timestamp,
                "metrics": metrics,
            }
            report["spore_id"] = spore_id
            self.reports[hostname] = report

    def merge(self, reports):
        normalized = [normalize_message(report) for report in reports]
        with self.lock:
            updates = {}
            accepted = []
            for report in normalized:
                hostname = report["hostname"]
                current = updates.get(hostname, self.reports.get(hostname))
                if current is None or report["timestamp"] > current["timestamp"]:
                    updates[hostname] = report
                    accepted.append(report)
            with self.connect_db() as conn:
                for report in accepted:
                    metrics = report["metrics"]
                    conn.execute(
                        """INSERT INTO telemetry_spores
                           (hostname, device_type, cpu_percent, mem_percent, disk_percent,
                            custom_metric, system_info_json, timestamp, spore_id)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (report["hostname"], report["device_type"], metrics["cpu_percent"],
                         metrics["mem_percent"], metrics["disk_percent"],
                         metrics["custom_metric"],
                         json.dumps({field: metrics[field] for field in SYSTEM_INFO_FIELDS}),
                         report["timestamp"], report["spore_id"]),
                    )
            self.reports.update(updates)
            return len(accepted)

    def snapshot(self):
        with self.lock:
            return [
                {key: dict(value) if key == "metrics" else value
                 for key, value in report.items() if key != "spore_id"}
                for report in self.reports.values()
            ]

    def spore_snapshot(self):
        with self.lock:
            return [report_to_spore(report) for report in self.reports.values()]

    def add_peers(self, urls):
        normalized = {peer_url(url) for url in urls}
        with self.lock:
            self.peers.update(normalized - {self.advertise_url})

    def peer_snapshot(self):
        with self.lock:
            return sorted(self.peers | {self.advertise_url})

    def record_peer_exchange(self, url, timestamp=None):
        normalized = peer_url(url)
        if normalized == self.advertise_url:
            return
        with self.lock:
            self.peers.add(normalized)
            self.peer_exchanges[normalized] = time.time() if timestamp is None else timestamp

    def topology_snapshot(self):
        with self.lock:
            peers = sorted(self.peers)
            return {
                "self": self.advertise_url,
                "peers": peers,
                "links": [
                    {"source": self.advertise_url, "target": url, "last_seen": timestamp}
                    for url, timestamp in sorted(self.peer_exchanges.items())
                ],
            }


def create_app(db_path=None, advertise_url="http://localhost:5000", seed_nodes=()):
    app = Flask(__name__)
    state = NodeState(
        db_path or os.environ.get("TELEMETRY_DB_PATH") or DEFAULT_DB_PATH,
        advertise_url, seed_nodes,
    )
    app.extensions["node_state"] = state

    @app.route("/")
    def index():
        return render_template("index.html")

    @app.route("/data")
    def data():
        now = time.time()
        return jsonify({
            report["hostname"]: {
                "device_type": report["device_type"],
                "metrics": report["metrics"],
                "last_updated": report["timestamp"],
                "status": report_status(report["timestamp"], now),
            }
            for report in state.snapshot()
        })

    @app.route("/topology")
    def topology():
        return jsonify(state.topology_snapshot())

    @app.route("/history")
    def history():
        hostname = flask_request.args.get("hostname", "").strip()
        if not hostname:
            return jsonify({"error": "hostname is required"}), 400
        try:
            hours = int(flask_request.args.get("hours", "24"))
        except ValueError:
            return jsonify({"error": "hours must be 1, 24, or 168"}), 400
        if hours not in (1, 24, 168):
            return jsonify({"error": "hours must be 1, 24, or 168"}), 400
        end = time.time()
        start = end - hours * 3600
        bucket_width = (end - start) / HISTORY_BUCKETS
        try:
            with closing(sqlite3.connect(state.db_path, timeout=5.0)) as conn:
                rows = conn.execute(
                    """SELECT AVG(timestamp), AVG(cpu_percent), AVG(mem_percent),
                              AVG(disk_percent)
                       FROM telemetry_spores
                       WHERE hostname = ? AND timestamp >= ? AND timestamp <= ?
                       GROUP BY MIN(CAST((timestamp - ?) / ? AS INTEGER), ?)
                       ORDER BY MIN(CAST((timestamp - ?) / ? AS INTEGER), ?)""",
                    (hostname, start, end, start, bucket_width, HISTORY_BUCKETS - 1,
                     start, bucket_width, HISTORY_BUCKETS - 1),
                ).fetchall()
        except sqlite3.Error:
            app.logger.exception("Failed to query telemetry history")
            return jsonify({"error": "Unable to load telemetry history"}), 500
        return jsonify({
            "hostname": hostname,
            "hours": hours,
            "points": [
                {
                    "timestamp": timestamp,
                    "cpu_percent": cpu,
                    "mem_percent": memory,
                    "disk_percent": disk,
                }
                for timestamp, cpu, memory, disk in rows
            ],
        })

    @app.route("/gossip", methods=["POST"])
    def gossip():
        reports = flask_request.get_json(silent=True)
        if not isinstance(reports, list):
            return jsonify({"error": "Request body must be an array of messages"}), 400
        sender = flask_request.headers.get("X-Peer-URL")
        try:
            if sender is not None:
                sender = peer_url(sender)
            accepted = state.merge(reports)
            if sender is not None:
                state.record_peer_exchange(sender)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except sqlite3.Error:
            app.logger.exception("Failed to persist gossip")
            return jsonify({"error": "Unable to persist gossip"}), 500
        return jsonify({"status": "success", "accepted": accepted, "peers": state.peer_snapshot()})

    return app


def collect_report(hostname, device_type, disk_path):
    memory = psutil.virtual_memory()
    disk = psutil.disk_usage(disk_path)
    temperatures = {}
    sensor_reader = getattr(psutil, "sensors_temperatures", None)
    if sensor_reader is not None:
        try:
            temperatures = sensor_reader()
        except NotImplementedError:
            pass

    cpu_temperatures = []
    gpu_temperatures = []
    for sensor_name, entries in temperatures.items():
        for entry in entries:
            if entry.current is None:
                continue
            labels = "{} {}".format(sensor_name, entry.label).lower()
            if any(name in labels for name in ("gpu", "amdgpu", "nouveau", "nvidia")):
                gpu_temperatures.append(entry.current)
            elif any(name in labels for name in ("cpu", "core", "package", "k10temp", "tctl")):
                cpu_temperatures.append(entry.current)

    cpu_temperature = max(cpu_temperatures) if cpu_temperatures else None
    gpu_temperature = max(gpu_temperatures) if gpu_temperatures else None
    os_name = platform.system()
    if os_name == "Windows":
        windows_temperatures = WINDOWS_SENSORS.read()
        cpu_temperature = windows_temperatures["cpu_temperature_c"]
        gpu_temperature = windows_temperatures["gpu_temperature_c"]
    if gpu_temperature is None:
        try:
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=temperature.gpu", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=1, check=True,
            )
        except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            pass
        else:
            readings = [
                float(line.strip()) for line in result.stdout.splitlines()
                if line.strip().isdigit()
            ]
            gpu_temperature = max(readings) if readings else None

    os_release = platform.version() if os_name == "Windows" else platform.release()
    return {
        "hostname": hostname,
        "device_type": device_type,
        "timestamp": time.time(),
        "metrics": {
            "cpu_percent": psutil.cpu_percent(interval=None),
            "mem_percent": memory.percent,
            "disk_percent": disk.percent,
            "cpu_count": psutil.cpu_count(logical=True),
            "mem_used_bytes": memory.used,
            "mem_total_bytes": memory.total,
            "disk_used_bytes": disk.used,
            "disk_total_bytes": disk.total,
            "cpu_temperature_c": cpu_temperature,
            "gpu_temperature_c": gpu_temperature,
            "os_version": "{} {}".format(os_name, os_release),
        },
    }


def gossip_once(state, timeout):
    peers = [url for url in state.peer_snapshot() if url != state.advertise_url]
    if not peers:
        return
    target = random.choice(peers)
    body = json.dumps(state.spore_snapshot(), allow_nan=False).encode("utf-8")
    http_request = request.Request(
        target + "/gossip", data=body,
        headers={"Content-Type": "application/json", "X-Peer-URL": state.advertise_url},
        method="POST",
    )
    try:
        with request.urlopen(http_request, timeout=timeout) as response:
            result = json.load(response)
    except error.HTTPError as exc:
        exc.close()
        raise
    if not isinstance(result, dict) or not isinstance(result.get("peers"), list):
        raise ValueError("peer returned an invalid gossip response")
    state.add_peers(result["peers"])
    state.record_peer_exchange(target)


def run(state, hostname, device_type, disk_path, interval, timeout, stop_event):
    psutil.cpu_percent(interval=None)
    if stop_event.wait(1):
        return
    while not stop_event.is_set():
        started = time.monotonic()
        try:
            report = normalize_report(collect_report(hostname, device_type, disk_path))
            report["spore_id"] = str(uuid.uuid4())
            state.merge([report_to_spore(report)])
        except (psutil.Error, OSError, sqlite3.Error, ValueError):
            LOGGER.exception("Could not collect or persist local telemetry")
        try:
            gossip_once(state, timeout)
        except (OSError, error.URLError, ValueError):
            LOGGER.exception("Could not exchange gossip")
        if stop_event.wait(max(0, interval - (time.monotonic() - started))):
            break


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed-nodes", nargs="*", required=True, type=cli_peer_url,
                        help="Initial peer URLs; leave empty to bootstrap the first node")
    parser.add_argument("--advertise-url", type=cli_peer_url,
                        help="This node's HTTP(S) URL as reachable by other nodes")
    parser.add_argument("--host", default="0.0.0.0", help="Listening address (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=5000, help="Listening port (default: 5000)")
    parser.add_argument("--hostname", default=socket.gethostname(), help="Telemetry device name")
    parser.add_argument("--device-type", default="server", help="Device type (default: server)")
    parser.add_argument("--disk-path", default=os.path.abspath(os.sep),
                        help="Path on the disk to monitor")
    parser.add_argument("--interval", type=positive_number, default=5.0,
                        help="Target seconds between collection and gossip (default: 5)")
    parser.add_argument("--timeout", type=positive_number, default=5.0,
                        help="HTTP timeout in seconds (default: 5)")
    args = parser.parse_args(argv)
    if not args.hostname.strip() or not args.device_type.strip():
        parser.error("--hostname and --device-type must not be empty")
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    advertise_url = args.advertise_url or "http://{}:{}".format(socket.gethostname(), args.port)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    app = create_app(advertise_url=advertise_url, seed_nodes=args.seed_nodes)
    state = app.extensions["node_state"]
    stop_event = threading.Event()
    worker = threading.Thread(
        target=run,
        args=(state, args.hostname, args.device_type, args.disk_path,
              args.interval, args.timeout, stop_event),
        daemon=True,
    )
    worker.start()
    LOGGER.info("Advertising node as %s", advertise_url)
    try:
        app.run(host=args.host, port=args.port, debug=False, use_reloader=False)
    finally:
        stop_event.set()
        worker.join(timeout=args.timeout + 1)
        WINDOWS_SENSORS.close()


if __name__ == "__main__":
    main()
