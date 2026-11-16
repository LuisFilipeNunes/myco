System Architecture
The stack relies on a Peer-to-Peer (P2P) Gossip Protocol. Instead of a central master, the network consists of autonomous nodes that measure their own state and periodically "gossip" (sync) their database with random neighbors. Information propagates organically across the network like a contagion.

Tier 1 Peers (Full Nodes - Windows & Linux):
A unified Python application that acts as both the sensor and the server. It runs seamlessly on your Windows 11 installation, natively on Debian, or containerized within Docker Swarm. Each Tier 1 peer runs:

The Sensor: Uses psutil to collect local hardware metrics. On Windows, the node manages a headless LibreHardwareMonitorLib helper to collect CPU/GPU temperatures without the Libre Hardware Monitor desktop app.

The Hive API: A lightweight Flask REST API that listens for gossip from other peers and serves the web dashboard.

Live State (RAM): Keeps the most recent payload for every known device on the network in memory.

Historical State (SQLite): Logs incoming payloads into a local telemetry.db, creating a permanent time-series record.

The Gossip Daemon: A background thread that wakes up, randomly selects another known Tier 1 peer, and pushes its latest state data.

Tier 2 Edge Nodes (Lightweight / Push-Only):
Low-power devices that do not have the resources to run Flask, SQLite, or participate in two-way gossip. They simply wake up, pick a random Tier 1 peer from a hardcoded list, push their metrics, and go to sleep.

ESP32s: An Arduino HTTP client pushes version 1 telemetry spores to a Tier 1 node's `/gossip` endpoint. The implementation and setup instructions are in `esp32/`; it maps heap usage to memory percentage and used/total memory, and reports free heap, Wi-Fi RSSI, and uptime in `custom_metric`. CPU and disk percentages are zero because this implementation does not measure them.

Phones/Tablets: Python scripts via Termux pushing battery levels and ARM CPU load.

The Client (Myco Dashboard):
A branded vanilla HTML/JS single-page application with Overview, Nodes, and History views. Overview summarizes node health and draws only peer links observed through successful gossip; Nodes supports searching and filtering while exposing available hardware information; History charts CPU, memory, and disk samples queried from that peer's SQLite database. Node health is derived from report freshness (online within 15 seconds, stale within 60 seconds, otherwise offline). Missing optional agent metrics display as unavailable rather than as zero. The dashboard adapts to phone screens without horizontal page scrolling, polls the peer's RAM cache, and requests a continuous Screen Wake Lock when supported.

The background extends behind the navigation and dashboard panels. To change it, place an SVG beside `node.py` and update the filename in the `--background-art` CSS variable in `templates/index.html`. Adjust `--background-art-opacity` to control its opacity.

Peer Implementation
Install the node dependencies with pip install -r requirements.txt, then run python node.py.

Each node listens on port 5000 (configurable). It requires a --seed-nodes parameter at startup (e.g., [http://192.168.1.10:5000](http://192.168.1.10:5000)) so it knows at least one other peer to begin gossiping with. Over time, peers share their node lists and discover the rest of the network automatically.

Start the first node with an empty seed list, and start other nodes with at least one reachable seed:

```
python node.py --seed-nodes --advertise-url http://192.168.1.10:5000
python node.py --seed-nodes http://192.168.1.10:5000 --advertise-url http://192.168.1.11:5000
```

Set `--advertise-url` to the URL other nodes can use to reach this node. If omitted, it defaults to the system hostname and configured port, which must be resolvable by peers. Set `--port`, `--host`, `--hostname`, `--device-type`, `--disk-path`, `--interval`, and `--timeout` as needed; see `python node.py --help`. Each node needs a distinct telemetry hostname and its own database. Set `TELEMETRY_DB_PATH` to select the database (default: `telemetry.db` beside `node.py`). The node stores spores in the `telemetry_spores` table; other tables in the database are left untouched and are not imported.

The node collects its own metrics and gossips every five seconds by default, after a one-second initial CPU sampling period. Failures are logged and retried on the next cycle. Each gossip request carries a JSON array of messages and advertises the sender in the `X-Peer-URL` header; the response includes known peer URLs for discovery. A request without this header can be used by a push-only edge device. An invalid batch is rejected as a whole; accepted newer reports are persisted before the live cache is updated. Equal or older timestamps are ignored, even when their spore IDs differ. Clocks should be synchronized across nodes for last-write-wins ordering; future-dated reports can suppress later updates from a source until its clock catches up.

Windows temperature capture

Build the bundled helper once using the .NET 10 SDK, from the project directory:

```powershell
dotnet publish .\windows-sensors\WindowsSensors.csproj -c Release -r win-x64 --self-contained true -o .\windows-sensors\publish
```

Keep the entire `windows-sensors\publish` directory beside the node when deploying. The self-contained build does not require a .NET runtime on the destination. The Python node automatically starts one hidden helper on its first Windows sample, reuses it for subsequent measurements, and closes it on shutdown. No Libre Hardware Monitor GUI, WMI namespace, or web server is required.

The helper enables only CPU and GPU hardware and reports the highest available temperature for each category in degrees Celsius. Some sensors require administrator privileges; run the node from an elevated terminal if necessary. Unsupported hardware or blocked drivers can still leave readings unavailable. Helper errors, timeouts, and missing sensors are logged without preventing CPU, memory, disk, or gossip updates. NVIDIA GPU temperatures also retain the `nvidia-smi` fallback. Linux continues to use psutil's kernel-backed sensors without the helper.

LibreHardwareMonitorLib is pinned through NuGet in the helper project. Its MPL-2.0 license and third-party notices apply; preserve the package's license notices when redistributing the published helper.

The API and peer discovery do not authenticate participants. Run nodes only on a trusted network or behind appropriately configured network access controls; discovered peers can cause outbound HTTP requests.

POST /gossip: Accepts a batch of telemetry spores. The receiving node checks each report's timestamp against the current report for its source. Newer reports are written to SQLite and update the RAM cache.

GET /data: Returns the current RAM cache (the entire network state as understood by this node).

GET /topology: Returns known peer URLs and the latest successful gossip-exchange time for each directly observed peer. Discovery alone does not create a connected topology edge.

GET /history?hostname=<node>&hours=1|24|168: Returns up to 240 time-bucketed average utilization samples for one telemetry source from this node's SQLite history.

/: Serves the polling dashboard. Browsers require a secure context for wake locks, so access over a LAN generally needs HTTPS (or localhost); the dashboard indicates when a wake lock is unavailable.

The MycoSpore Data Contract
A MycoSpore is one message, not an entire gossip snapshot. Version 1 supports only `telemetry`; unknown versions and types are rejected with HTTP 400. Each gossip batch is an array of messages. This is latest-state gossip, not guaranteed delivery of every historical measurement.

```json
[
  {
    "version": 1,
    "spore_id": "a9bdde39-2bf3-4f4d-90a0-898f4ae488d8",
    "source_id": "debian-node-1",
    "type": "telemetry",
    "timestamp": "2026-11-16T02:00:00Z",
    "payload": {
      "device_type": "server",
      "cpu_percent": 14.5,
      "mem_percent": 42.1,
      "disk_percent": 68.0,
      "custom_metric": "optional",
      "cpu_count": 8,
      "mem_used_bytes": 18038862643,
      "mem_total_bytes": 34359738368,
      "disk_used_bytes": 128849018880,
      "disk_total_bytes": 536870912000,
      "cpu_temperature_c": 52.5,
      "gpu_temperature_c": 61.0,
      "os_version": "Linux 6.1"
    }
  }
]
```

`source_id` is the telemetry hostname and must uniquely and consistently identify a device. The originating device generates a unique `spore_id` for each measurement and retains it when retrying; full nodes use UUIDs and preserve IDs through gossip and restarts. IDs are non-empty strings. Telemetry retries are effectively deduplicated by the source's timestamp, not by a general-purpose message ID ledger. Timestamps are origin-generated UTC dates ending in `Z`, with up to 324 fractional digits; extended precision preserves floating-point timestamps through serialization. The three percentage metrics are required numbers between 0 and 100; `custom_metric` is an optional string or finite number. The system-information fields in the example are optional for compatibility with edge clients and older peers. `cpu_count` is the logical-core count; memory and disk capacities are byte counts; temperature values are degrees Celsius. Temperatures and system information not available on a device can be omitted. Full nodes display unavailable values as a dash and persist system information across restarts.

Lightweight edge clients can produce messages under this contract independently of the full-node implementation.

Execution Roadmap
Phase 1: The Colony Node (Tonight's Goal)

The Unified Node: Write node.py, which merges the Flask server and the psutil data collection into a single application.

The Gossip Thread: Implement the logic where the node periodically POSTs its known data to a random neighbor.

The Dashboard: Build the HTML/JS frontend with auto-refresh logic and the Wake Lock API.

Phase 2: Swarm Deployment & Data Analysis
4.  Containerization: Write a Dockerfile for the Node to deploy it into your Portainer/Docker Swarm setup.
5.  Service Persistence: Set up Windows Task Scheduler to keep the Windows node running cleanly in the background.
6.  Historical Export: Write a quick script to query any node's SQLite DB and export the data to CSV for charting.

Phase 3: The Edge (Future Enhancements)
7.  IoT Integration: Adapt the payload for Sentinel or Auto Home ESP32s so they can push to the colony. A baseline Arduino ESP32 push client is implemented in `esp32/`.
8.  Termux Edge Node: Map Android battery and sensor data to the payload using Termux:API.