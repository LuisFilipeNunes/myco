# ESP32 push client

This PlatformIO Arduino sketch sends telemetry spores to a Tier 1 node's
`POST /gossip` endpoint. It does not run Flask, SQLite, or peer discovery.
The existing `node.py` is already suitable for Linux; use its regular
installation and service management there rather than maintaining a second
Linux implementation.

## Configure and build

1. Install PlatformIO and the Espressif32 platform toolchain.
2. Copy `include/config.example.h` to `include/config.h`.
3. Set the Wi-Fi credentials, a reachable node base URL, and a stable unique
   `SOURCE_ID`. `NODE_URL` is the base URL without `/gossip`.
4. Build and upload from this directory:

   ```sh
   pio run
   pio run --target upload
   pio device monitor
   ```

The sketch uses NTP to create UTC timestamps. It waits for a valid time before
creating a spore. Each spore is retained unchanged in RAM until a successful
HTTP response, so retries keep the same ID and timestamp. A reset loses any
unsent spore; the next report receives a new ID.

## ESP32 metric mapping

- `cpu_percent` is `0` because the Arduino APIs used here do not provide CPU
  utilization.
- `mem_percent` is used heap as a percentage of total heap.
- `mem_used_bytes` and `mem_total_bytes` report used and total heap so the
  dashboard can show the ESP32's memory capacity.
- `disk_percent` is `0` because this client does not mount or measure a
  filesystem.
- `custom_metric` contains `free_heap_bytes`, `wifi_rssi_dbm`, and
  `uptime_seconds`.

The node requires the three percentage fields even when a device cannot measure
them. CPU/GPU temperatures, storage capacity, and OS version are not available
from this sketch and appear as unavailable on the dashboard. Keep each ESP32's
`SOURCE_ID` stable and unique. The node API has no participant authentication;
use this client only on a trusted network or with network access controls, and
do not expose plain HTTP to an untrusted network.
