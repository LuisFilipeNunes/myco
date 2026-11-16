# Myco

Myco is a peer-to-peer telemetry network. Python full nodes collect system
metrics, exchange them through a gossip protocol, persist telemetry in SQLite,
and serve a web dashboard. An ESP32 client can publish lightweight telemetry to
a full node.

## Project layout

- `node.py` - full-node application and command-line entry point
- `windows_sensors.py` - Windows temperature-helper integration
- `templates/` and `static/` - dashboard UI and assets
- `tests/` - Python test suite
- `windows-sensors/` - .NET temperature helper
- `esp32/` - PlatformIO telemetry client
- `docs/` - design and architecture notes
- `scripts/` - optional local diagnostic utilities

## Run a node

Create a virtual environment, install the dependencies, and start the first
node with an empty seed list:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python node.py --seed-nodes
```

Open <http://localhost:5000>.

## Test

```powershell
python -m unittest discover -s tests
```

See [the application specifications](docs/app_specifications.md) and the
[ESP32 guide](esp32/README.md) for more detail.
