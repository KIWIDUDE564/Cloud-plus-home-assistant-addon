import json
import logging
import os
import time
from typing import Any, Dict, Optional

import paho.mqtt.client as mqtt
import requests

BASE_URL = "http://smartgencloudplus.cn:8082"
STATUS_URL = f"{BASE_URL}/devicedata/getstatus"
ACTION_URL = f"{BASE_URL}/devicedata/sendaction"
OPTIONS_PATH = "/data/options.json"
DEFAULT_POLL_INTERVAL = 3


class SmartGenClient:
    def __init__(self, config: Dict[str, Any]) -> None:
        self.base_url = BASE_URL
        self.token = config.get("token", "")
        self.utoken = config.get("utoken", "")
        self.address = str(config.get("genset_address", ""))
        self.language = config.get("language", "en-US")
        self.timezone = config.get("timezone", "Pacific/Honolulu")
        self.session = requests.Session()

    def _post(self, url: str, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        payload = {
            "address": self.address,
            "language": self.language,
            "timezone": self.timezone,
            "token": self.token,
            "utoken": self.utoken,
            **data,
        }
        try:
            response = self.session.post(url, data=payload, timeout=10)
            logging.debug("SmartGen status HTTP: %s", response.status_code)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as err:
            logging.warning("SmartGen request failed for %s: %s", url, err)
        except json.JSONDecodeError:
            logging.warning("SmartGen request returned non-JSON response for %s", url)
        return None

    def send_action(self, act: str) -> Optional[Dict[str, Any]]:
        logging.info("Sending SmartGen action: %s", act)
        return self._post(ACTION_URL, {"act": act})

    def get_status(self) -> Optional[Dict[str, Any]]:
        for attempt in range(3):
            result = self._post(STATUS_URL, {})
            if result is not None:
                logging.debug("Received status payload")
                return result
            backoff = 2 ** attempt
            logging.debug("Retrying status fetch in %s seconds", backoff)
            time.sleep(backoff)
        return None


def load_config(path: str = OPTIONS_PATH) -> Dict[str, Any]:
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as file:
            return json.load(file)
    logging.warning("Options file not found at %s, using defaults", path)
    return {}


def to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.lower() in {"true", "on", "yes", "1"}
    return False


def coalesce(data: Dict[str, Any], keys: list, default: Any = None) -> Any:
    for key in keys:
        if key in data and data[key] not in (None, ""):
            return data[key]
    return default


class SmartGenBridge:
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.client = SmartGenClient(config)
        self.poll_interval = int(config.get("poll_interval", DEFAULT_POLL_INTERVAL))
        self.mqtt_base = config.get("mqtt_base_topic", "smartgen")
        self.address = str(config.get("genset_address", ""))
        self.availability_topic = f"{self.base_topic}/availability"
        self.command_map = {
            f"{self.base_topic}/command/start": "start",
            f"{self.base_topic}/command/stop": "stop",
            f"{self.base_topic}/command/auto": "auto",
            f"{self.base_topic}/command/manual": "manual",
            f"{self.base_topic}/command/genset_closeopen": "gensetcloseopen",
            f"{self.base_topic}/command/mains_closeopen": "maincloseopen",
        }
        self.mqtt_client = mqtt.Client()
        self.mqtt_client.on_connect = self.on_connect
        self.mqtt_client.on_message = self.on_message
        self.mqtt_client.on_disconnect = self.on_disconnect

    @property
    def base_topic(self) -> str:
        return f"{self.mqtt_base}/{self.address}"

    @property
    def device_info(self) -> Dict[str, Any]:
        return {
            "identifiers": [f"smartgen_{self.address}"],
            "manufacturer": "SmartGen",
            "model": "Cloud Plus",
            "name": f"SmartGen {self.address}",
        }

    def resolve_mqtt_settings(self) -> Dict[str, Any]:
        """Resolve MQTT connection details from Supervisor service if available."""
        supervisor_token = os.getenv("SUPERVISOR_TOKEN")
        supervisor_endpoint = os.getenv("SUPERVISOR_ENDPOINT", "http://supervisor")

        if supervisor_token:
            try:
                response = requests.get(
                    f"{supervisor_endpoint}/services/mqtt",
                    headers={"Authorization": f"Bearer {supervisor_token}"},
                    timeout=5,
                )
                response.raise_for_status()
                data = response.json()
                logging.info("Using MQTT service details from Supervisor")
                return {
                    "host": data.get("host", self.config.get("mqtt_host", "core-mosquitto")),
                    "port": int(data.get("port", self.config.get("mqtt_port", 1883))),
                    "username": data.get("username") or self.config.get("mqtt_username"),
                    "password": data.get("password") or self.config.get("mqtt_password"),
                }
            except requests.RequestException as err:
                logging.warning("Failed to load MQTT details from Supervisor: %s", err)

        return {
            "host": self.config.get("mqtt_host", "core-mosquitto"),
            "port": int(self.config.get("mqtt_port", 1883)),
            "username": self.config.get("mqtt_username"),
            "password": self.config.get("mqtt_password"),
        }

    def connect_mqtt(self) -> None:
        mqtt_settings = self.resolve_mqtt_settings()
        host = mqtt_settings.get("host", "core-mosquitto")
        port = int(mqtt_settings.get("port", 1883))
        username = mqtt_settings.get("username")
        password = mqtt_settings.get("password")

        if username:
            self.mqtt_client.username_pw_set(username, password)

        self.mqtt_client.will_set(self.availability_topic, "offline", retain=True)

        logging.info("Connecting to MQTT broker %s:%s", host, port)
        self.mqtt_client.connect(host, port, keepalive=60)
        self.mqtt_client.loop_start()

    def on_connect(self, client: mqtt.Client, userdata: Any, flags: Dict[str, Any], rc: int):
        if rc == 0:
            logging.info("Connected to MQTT broker")
            for topic in self.command_map:
                client.subscribe(topic)
            client.publish(self.availability_topic, "online", retain=True)
            self.publish_discovery()
        else:
            logging.warning("Failed to connect to MQTT broker, rc=%s", rc)

    def on_disconnect(self, client: mqtt.Client, userdata: Any, rc: int):
        logging.warning("MQTT disconnected (rc=%s), attempting reconnect", rc)
        try:
            client.reconnect()
        except Exception as err:  # noqa: BLE001
            logging.error("MQTT reconnect failed: %s", err)

    def on_message(self, client: mqtt.Client, userdata: Any, msg: mqtt.MQTTMessage):
        topic = msg.topic
        act = self.command_map.get(topic)
        if not act:
            return
        logging.info("Received command on %s -> %s", topic, act)
        self.client.send_action(act)

    def publish_discovery(self) -> None:
        discovery_base = "homeassistant"
        retain = True

        def publish_config(domain: str, object_id: str, payload: Dict[str, Any]):
            topic = f"{discovery_base}/{domain}/smartgen_{self.address}_{object_id}/config"
            self.mqtt_client.publish(topic, json.dumps(payload), retain=retain)

        availability = [{"topic": self.availability_topic}]

        switch_definitions = {
            "start": {"name": "Start", "icon": "mdi:power"},
            "stop": {"name": "Stop", "icon": "mdi:stop"},
            "auto": {"name": "Auto Mode", "icon": "mdi:alpha-a-circle-outline"},
            "manual": {"name": "Manual Mode", "icon": "mdi:hand"},
            "genset_closeopen": {"name": "Genset Breaker", "icon": "mdi:transmission-tower"},
            "mains_closeopen": {"name": "Mains Breaker", "icon": "mdi:transmission-tower-export"},
        }

        for key, meta in switch_definitions.items():
            payload = {
                "name": f"SmartGen {self.address} {meta['name']}",
                "unique_id": f"smartgen_{self.address}_{key}",
                "command_topic": f"{self.base_topic}/command/{key}",
                "payload_on": "ON",
                "payload_off": "OFF",
                "device": self.device_info,
                "icon": meta["icon"],
                "availability": availability,
                "availability_mode": "latest",
            }
            publish_config("switch", key, payload)

        sensors = [
            ("rpm", "RPM", "mdi:engine", "rpm", None),
            ("frequency_hz", "Frequency", "mdi:flash", "Hz", "frequency"),
            ("voltage_l1_l2", "Voltage L1-L2", "mdi:flash", "V", None),
            ("voltage_l2_l3", "Voltage L2-L3", "mdi:flash", "V", None),
            ("voltage_l3_l1", "Voltage L3-L1", "mdi:flash", "V", None),
            ("kw", "Active Power", "mdi:transmission-tower", "kW", "power"),
            ("run_hours", "Run Hours", "mdi:timer-outline", "h", None),
            ("alarms_active", "Active Alarms", "mdi:alert", None, None),
        ]

        for key, name, icon, unit, device_class in sensors:
            payload = {
                "name": f"SmartGen {self.address} {name}",
                "state_topic": f"{self.base_topic}/{key}",
                "unique_id": f"smartgen_{self.address}_{key}",
                "device": self.device_info,
                "icon": icon,
                "availability": availability,
                "availability_mode": "latest",
            }
            if unit:
                payload["unit_of_measurement"] = unit
            if device_class:
                payload["device_class"] = device_class
            publish_config("sensor", key, payload)

        binaries = [
            ("running", "Running", "mdi:engine-outline"),
            ("alarm", "Alarm Present", "mdi:alert"),
            ("mains_available", "Mains Available", "mdi:flash"),
            ("genset_breaker_closed", "Genset Breaker Closed", "mdi:transmission-tower"),
            ("mains_breaker_closed", "Mains Breaker Closed", "mdi:transmission-tower-export"),
            ("auto_mode", "Auto Mode", "mdi:alpha-a-circle"),
            ("manual_mode", "Manual Mode", "mdi:hand"),
        ]

        for key, name, icon in binaries:
            payload = {
                "name": f"SmartGen {self.address} {name}",
                "state_topic": f"{self.base_topic}/{key}",
                "unique_id": f"smartgen_{self.address}_{key}",
                "device_class": "power" if key in {"running", "mains_available"} else None,
                "payload_on": "ON",
                "payload_off": "OFF",
                "device": self.device_info,
                "icon": icon,
                "availability": availability,
                "availability_mode": "latest",
            }
            payload = {k: v for k, v in payload.items() if v is not None}
            publish_config("binary_sensor", key, payload)

    def publish_status(self, status: Dict[str, Any]) -> None:
        parsed = self.parse_status(status)

        topics = {
            "state": parsed["state_text"],
            "rpm": parsed["rpm"],
            "frequency_hz": parsed["frequency_hz"],
            "voltage_l1_l2": parsed["voltage_l1_l2"],
            "voltage_l2_l3": parsed["voltage_l2_l3"],
            "voltage_l3_l1": parsed["voltage_l3_l1"],
            "kw": parsed["kw"],
            "run_hours": parsed["run_hours"],
            "alarms_active": parsed["alarm_count"],
            "running": "ON" if parsed["running"] else "OFF",
            "alarm": "ON" if parsed["alarm_present"] else "OFF",
            "mains_available": "ON" if parsed["mains_available"] else "OFF",
            "genset_breaker_closed": "ON" if parsed["genset_breaker_closed"] else "OFF",
            "mains_breaker_closed": "ON" if parsed["mains_breaker_closed"] else "OFF",
            "auto_mode": "ON" if parsed["auto_mode"] else "OFF",
            "manual_mode": "ON" if parsed["manual_mode"] else "OFF",
        }

        for suffix, value in topics.items():
            topic = f"{self.base_topic}/{suffix}"
            self.mqtt_client.publish(topic, value, retain=False)

        telemetry_topic = f"{self.base_topic}/telemetry"
        self.mqtt_client.publish(telemetry_topic, json.dumps(status), retain=False)

    def parse_status(self, status: Dict[str, Any]) -> Dict[str, Any]:
        # Attempt to normalize values from various possible field names.
        rpm = coalesce(status, ["rpm", "RPM", "speed", "gensetrpm"], 0)
        frequency = coalesce(status, ["hz", "frequency", "Frequency"], 0)
        voltage_l1_l2 = coalesce(status, ["voltage_l1_l2", "uab", "Uab", "u_ab", "ua"] , 0)
        voltage_l2_l3 = coalesce(status, ["voltage_l2_l3", "ubc", "Ubc", "u_bc", "ub"], 0)
        voltage_l3_l1 = coalesce(status, ["voltage_l3_l1", "uca", "Uca", "u_ca", "uc"], 0)
        kw = coalesce(status, ["kw", "power_kw", "active_power", "kW"], 0)
        run_hours = coalesce(status, ["run_hours", "runtime", "TotalRunTime", "runhour", "runtotal"], 0)

        alarm_list = status.get("alarms") or status.get("alarm_list") or status.get("AlarmList") or []
        alarm_count = len(alarm_list) if isinstance(alarm_list, list) else int(alarm_list or 0)
        alarm_present = alarm_count > 0 or to_bool(status.get("alarm"))

        running = to_bool(coalesce(status, ["running", "run_state", "runState", "is_running", "Run"], False))
        auto_mode = to_bool(coalesce(status, ["auto", "auto_mode", "AutoMode", "IsAuto"], False))
        manual_mode = to_bool(coalesce(status, ["manual", "manual_mode", "ManualMode"], False))
        mains_available = to_bool(coalesce(status, ["mains", "mains_available", "GridAvailable", "MainsAvailable"], False))
        genset_breaker_closed = to_bool(coalesce(status, ["genset_breaker", "genset_breaker_closed", "GenBreaker", "GenBreakerClosed"], False))
        mains_breaker_closed = to_bool(coalesce(status, ["mains_breaker", "mains_breaker_closed", "MainsBreaker", "MainsBreakerClosed"], False))

        state_text = "running" if running else "stopped"
        if alarm_present:
            state_text = "alarm"

        return {
            "rpm": rpm,
            "frequency_hz": frequency,
            "voltage_l1_l2": voltage_l1_l2,
            "voltage_l2_l3": voltage_l2_l3,
            "voltage_l3_l1": voltage_l3_l1,
            "kw": kw,
            "run_hours": run_hours,
            "alarm_count": alarm_count,
            "alarm_present": alarm_present,
            "running": running,
            "auto_mode": auto_mode,
            "manual_mode": manual_mode,
            "mains_available": mains_available,
            "genset_breaker_closed": genset_breaker_closed,
            "mains_breaker_closed": mains_breaker_closed,
            "state_text": state_text,
        }

    def loop(self) -> None:
        self.connect_mqtt()
        while True:
            try:
                status = self.client.get_status()
                if status:
                    self.publish_status(status)
            except Exception as err:  # noqa: BLE001
                logging.error("Unexpected error: %s", err)
            time.sleep(self.poll_interval)


def configure_logging(level: str) -> None:
    numeric_level = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(
        level=numeric_level,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )


def main() -> None:
    config = load_config()
    configure_logging(config.get("log_level", "info"))
    if not config.get("token") or not config.get("utoken"):
        logging.warning("Token or utoken is empty. Please update the add-on configuration.")
    bridge = SmartGenBridge(config)
    bridge.loop()


if __name__ == "__main__":
    main()
