# SmartGen Cloud Bridge Home Assistant Add-on

This add-on connects the SmartGen Cloud Plus API to Home Assistant using MQTT discovery. It polls the generator for live data with the SmartGen mobile API (token/utoken authentication and `X-Sign` header) and exposes sensors, binary sensors, and command switches so you can control and monitor your genset from Home Assistant. A sample Lovelace dashboard is included for a quick UI start. The bridge will automatically adopt the Supervisor-provided MQTT service details when available.

## Features
- Polls SmartGen Cloud Plus status and publishes telemetry to MQTT.
- Auto-discovers entities in Home Assistant via MQTT discovery.
- Switch entities for start/stop, auto/manual, and breaker open/close commands.
- Sensor entities for RPM, frequency, voltages, kW, run hours, and alarm count.
- Binary sensors for running state, mains availability, breaker states, and alarm presence.
- Example Lovelace dashboard with gauges, status chips, and command buttons.

## Installation
1. Add this repository as a custom add-on repository in Home Assistant.
2. Install the **SmartGen Cloud Bridge** add-on.
3. Configure the add-on options (see below) and start the add-on.

## Configuration options
All options live in `/data/options.json` managed by the Supervisor:

| Option | Description | Default |
| --- | --- | --- |
| `genset_address` | Generator address/id from SmartGen Cloud | `"7049"` |
| `language` | Language code for API calls | `"en-US"` |
| `timezone` | Time zone string for API calls | `"Asia/Shanghai"` |
| `token` | Captured SmartGen Cloud token | `""` |
| `utoken` | Captured SmartGen Cloud utoken | `""` |
| `poll_interval` | Seconds between status polls | `30` |
| `mqtt_host` | MQTT broker hostname (Supervisor service is `core-mosquitto`; overridden automatically if Supervisor MQTT service is discovered) | `"core-mosquitto"` |
| `mqtt_port` | MQTT broker port | `1883` |
| `mqtt_username` | MQTT username (if required) | `""` |
| `mqtt_password` | MQTT password (if required) | `""` |
| `mqtt_base_topic` | Root MQTT topic for publishing | `"smartgen"` |
| `log_level` | Logging level (`debug`, `info`, `warning`, `error`) | `"info"` |

### Authentication
The add-on uses captured SmartGen Cloud `token` and `utoken` from the mobile app; no login flow is performed. Requests target `http://smartgencloudplus.cn:8082/devicedata/getstatus` and `/devicedata/sendaction` with form-encoded payloads including `address`, `language`, `timezone`, `token`, and `utoken`. Each request sends `User-Agent: okhttp/4.9.0`, `Content-Type: application/x-www-form-urlencoded`, and `X-Sign`, where `X-Sign = md5(f"{address}{token}{utoken}smartgen")`. HTML responses are detected and skipped to avoid JSON errors.

## MQTT entities
Entities are published under `<mqtt_base_topic>/<genset_address>` (e.g., `smartgen/7049`). MQTT discovery is used to register the following Home Assistant entities, all of which include an availability topic:

- Switches: start, stop, auto mode, manual mode, genset breaker close/open, mains breaker close/open.
- Sensors: RPM, frequency, voltages (L1-L2, L2-L3, L3-L1), kW, run hours, active alarms.
- Binary sensors: running, alarm present, mains available, genset breaker closed, mains breaker closed, auto mode, manual mode.
- Telemetry payload: raw status JSON published to `<base>/telemetry`.
- Additional direct topics published under `smartgen/generator/` for simple consumers: `status`, `voltage`, `frequency`, `runtime`, `battery`, `alarms`, and `power_kw`.

## Sample Lovelace dashboard
The `dashboard/smartgen_dashboard.yaml` file provides a ready-made Lovelace view that uses the discovered entities. Update the entity IDs if you change the genset address.

To import:
1. In Home Assistant, go to **Settings → Dashboards → Three-dot menu → Raw configuration editor**.
2. Paste the contents of `dashboard/smartgen_dashboard.yaml` (adjusting entity IDs as needed).
3. Save and reload the dashboard.

## Development
The add-on runs `smartgen_bridge.py` inside a Python 3.12 Alpine container. Dependencies are listed in `requirements.txt` and installed via `pip`.

### Running locally
```bash
docker build -t smartgen-cloud-bridge .
docker run --rm -it -v $(pwd)/data:/data smartgen-cloud-bridge
```

Ensure `data/options.json` exists locally with the same structure as the Home Assistant options file.

## Notes
- Tokens are never logged. Debug logging only includes non-sensitive payload information.
- The add-on will retry API calls with simple exponential backoff on failures.
- MQTT discovery messages are retained; telemetry/state messages are not.
- An availability topic (`<base>/availability`) is published for Home Assistant entities so the dashboard reflects MQTT connectivity.
- SmartGen Cloud Plus API requests use the mobile endpoints `/devicedata/getstatus` and `/devicedata/sendaction` with token/utoken signing (no login flow required).
- Supervisor MQTT discovery requests include the Supervisor token and gracefully fall back to manual MQTT settings if access is denied.
