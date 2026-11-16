# MycoSpore

A **MycoSpore** is a single, versioned message that carries one telemetry
measurement through the Myco network. It is the smallest unit exchanged and
persisted by participating nodes. A MycoSpore is not a complete network
snapshot.

## Version 1 contract

```json
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
    "custom_metric": "warm",
    "cpu_count": 8,
    "mem_used_bytes": 17179869184,
    "mem_total_bytes": 34359738368,
    "disk_used_bytes": 128849018880,
    "disk_total_bytes": 536870912000,
    "cpu_temperature_c": 52.5,
    "gpu_temperature_c": 61.0,
    "os_version": "Linux 6.1"
  }
}
```

## Envelope fields

| Field | Requirement | Meaning |
| --- | --- | --- |
| `version` | Required integer | Contract version. The current and only accepted value is `1`. |
| `spore_id` | Required non-empty string | Unique identity generated for the measurement. A retry must retain the same value. |
| `source_id` | Required non-empty string | Stable, unique identity of the originating device. Leading or trailing whitespace is invalid. |
| `type` | Required string | Message type. Version 1 accepts only `telemetry`. |
| `timestamp` | Required string | Origin-generated UTC timestamp ending in `Z`. |
| `payload` | Required object | Telemetry values produced by the source. |

The timestamp uses the form `YYYY-MM-DDTHH:MM:SSZ` and may contain a
fractional part before `Z`. Fractional timestamps may use up to 324 digits so
values can survive serialization without creating false updates. Calendar
dates must be valid; offsets such as `+00:00` are not accepted in place of
`Z`.

## Telemetry payload

The following payload fields are required:

| Field | Valid value |
| --- | --- |
| `device_type` | Non-empty string describing the source, such as `server` or `esp32`. |
| `cpu_percent` | Finite number from `0` to `100`. |
| `mem_percent` | Finite number from `0` to `100`. |
| `disk_percent` | Finite number from `0` to `100`. |

The following fields are optional:

| Field | Valid value |
| --- | --- |
| `custom_metric` | String or finite number. An omitted value is treated as an empty string. |
| `cpu_count` | Positive integer representing logical CPU cores. |
| `mem_used_bytes` | Non-negative finite number. |
| `mem_total_bytes` | Non-negative finite number. |
| `disk_used_bytes` | Non-negative finite number. |
| `disk_total_bytes` | Non-negative finite number. |
| `cpu_temperature_c` | Non-negative finite number in degrees Celsius. |
| `gpu_temperature_c` | Non-negative finite number in degrees Celsius. |
| `os_version` | String describing the operating system. |

When both used and total capacity are present, the used value cannot exceed
the total. A source should omit optional measurements that it cannot obtain
instead of inventing a value.

## Identity, retries, and ordering

The source creates a new `spore_id` for each new measurement. If delivery
fails, it retries the exact same MycoSpore, preserving both `spore_id` and
`timestamp`.

Updates are ordered per `source_id` using `timestamp`. A receiver accepts and
persists a MycoSpore only when its timestamp is newer than the latest accepted
timestamp for that source. Equal or older messages are ignored, even if their
`spore_id` values differ. Therefore, synchronized source clocks are important:
a future-dated MycoSpore can prevent otherwise valid updates from being
accepted until the source clock catches up.

The `spore_id` preserves measurement identity across retries, forwarding, and
restarts, but it is not used as a general-purpose deduplication ledger.

## Exchange rules

MycoSpores are exchanged as a JSON array, including when the array contains
only one message:

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
      "disk_percent": 68.0
    }
  }
]
```

The entire array is validated before any MycoSpore is accepted. If one entry
is invalid, the whole batch is rejected. Valid but equal or older entries do
not invalidate the batch; they are simply ignored under the ordering rule.

MycoSpore exchange propagates the latest known state for each source. It does
not guarantee delivery of every historical measurement.
