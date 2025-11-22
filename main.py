import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple

import paho.mqtt.client as mqtt
import requests

OPTIONS_PATH = "/data/options.json"
DEFAULT_API_BASE = "https://smartgencloudplus.cn:8082"
DEFAULT_POLL_INTERVAL = 30
DEFAULT_BASE_TOPIC = "smartgen"
HEARTBEAT_INTERVAL = 60
REQUEST_TIMEOUT = 20


def load_config(path: str = OPTIONS_PATH) -> Dict[str, Any]:
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as file:
                return json.load(file)
    except Exception as err:  # noqa: BLE001
        logging.warning("Failed to read config from %s: %s", path, err)
    return {}


def configure_logging(level: str) -> None:
    numeric_level = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(level=numeric_level, format="%(asctime)s [%(levelname)s] %(message)s")


class JsonParser:
    @staticmethod
    def parse_response(response: requests.Response) -> Dict[str, Any]:
        try:
            return response.json()
        except Exception:  # noqa: BLE001
            text = response.text if response is not None else ""
            logging.warning("Failed JSON parse; attempting regex extraction (status=%s)", getattr(response, "status_code", "?"))
            candidate = JsonParser.extract_json_like(text)
            if candidate:
                try:
                    return json.loads(candidate)
                except Exception as err:  # noqa: BLE001
                    logging.warning("Secondary JSON load failed: %s", err)
            if text:
                logging.debug("Raw response body (truncated): %s", text[:500])
        return {}

    @staticmethod
    def extract_json_like(text: str) -> Optional[str]:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        return match.group(0) if match else None


class SmartGenApiClient:
    def __init__(self, config: Dict[str, Any]):
        self.session = requests.Session()
        self.base_url = (config.get("api_base") or DEFAULT_API_BASE).rstrip("/")
        self.username = config.get("username") or config.get("user") or ""
        self.password = config.get("password") or config.get("pass") or ""
        self.token = config.get("token") or ""
        self.utoken = config.get("utoken") or ""
        self.device_id = str(config.get("genset_address") or config.get("address") or "unknown")
        self.language = config.get("language", "en-US")
        self.timezone = config.get("timezone", "UTC")
        self.paths = ["/user/login", "/user/info", "/genset/mylist", "/genset/list", "/genset/getNav"]

    def _url(self, path: str) -> str:
        normalized = path if path.startswith("/") else f"/{path}"
        return f"{self.base_url}{normalized}"

    def _send_request(self, path: str, payload: Dict[str, Any]) -> Tuple[bool, Dict[str, Any]]:
        url = self._url(path)
        try:
            logging.debug("POST %s", url)
            response = self.session.post(url, data=payload, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as err:
            logging.error("Network error calling %s: %s", url, err)
            return False, {}

        logging.debug("HTTP %s -> %s", response.status_code, url)
        content_type = response.headers.get("Content-Type", "").lower()
        body = response.text[:500]
        if "text/html" in content_type or body.strip().startswith("<"):
            logging.warning("Received HTML response from %s; skipping JSON parse", url)
            return False, {}

        data = JsonParser.parse_response(response)
        self._update_tokens(data)
        return True, data

    def _update_tokens(self, data: Dict[str, Any]) -> None:
        token = data.get("token") or data.get("Token")
        utoken = data.get("utoken") or data.get("uToken") or data.get("UToken")
        if token:
            self.token = token
            logging.info("Updated token from response")
        if utoken:
            self.utoken = utoken
            logging.info("Updated utoken from response")

    def _base_payload(self) -> Dict[str, Any]:
        payload = {
            "token": self.token,
            "utoken": self.utoken,
            "address": self.device_id,
            "language": self.language,
            "timezone": self.timezone,
        }
        if self.username:
            payload["username"] = self.username
        if self.password:
            payload["password"] = self.password
        return {k: v for k, v in payload.items() if v is not None}

    def fetch_snapshot(self) -> Dict[str, Any]:
        payload = self._base_payload()
        for path in self.paths:
            ok, data = self._send_request(path, payload)
            if not ok or not data:
                continue
            normalized = self._normalize_payload(data)
            if normalized:
                return normalized
        logging.error("All API endpoints failed; returning empty snapshot")
        return {}

    def _normalize_payload(self, data: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(data, dict):
            return {}
        candidate = data.get("data") if isinstance(data.get("data"), (dict, list)) else data
        if isinstance(candidate, list):
            candidate = candidate[0] if candidate else {}
        result = candidate if isinstance(candidate, dict) else {}
        result.setdefault("device_id", self.device_id)
        return result


class SmartGenMqttBridge:
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.base_topic = config.get("base_topic") or config.get("mqtt_base_topic") or DEFAULT_BASE_TOPIC
        self.device_id = str(config.get("genset_address") or config.get("address") or "unknown")
        self.poll_interval = int(config.get("poll_interval", DEFAULT_POLL_INTERVAL))
        self.client = mqtt.Client()
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.will_set(f"{self.base_topic}/status", "offline", qos=1, retain=True)

    def connect(self) -> None:
        settings = self._resolve_mqtt_settings()
        if settings.get("username"):
            self.client.username_pw_set(settings.get("username"), settings.get("password"))
        while True:
            try:
                logging.info("Connecting to MQTT %s:%s", settings.get("host"), settings.get("port"))
                self.client.connect(settings.get("host", "core-mosquitto"), int(settings.get("port", 1883)), keepalive=60)
                self.client.loop_start()
                break
            except Exception as err:  # noqa: BLE001
                logging.error("MQTT connection failed: %s", err)
                time.sleep(5)

    def _resolve_mqtt_settings(self) -> Dict[str, Any]:
        host = self.config.get("mqtt_host", "core-mosquitto")
        port = int(self.config.get("mqtt_port", 1883))
        username = self.config.get("mqtt_username") or None
        password = self.config.get("mqtt_password") or None
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
                host = data.get("host", host)
                port = int(data.get("port", port))
                username = data.get("username") or username
                password = data.get("password") or password
                logging.info("Using MQTT details from Supervisor API")
            except requests.RequestException as err:
                logging.warning("Falling back to manual MQTT settings: %s", err)
        return {"host": host, "port": port, "username": username, "password": password}

    def _on_connect(self, client: mqtt.Client, userdata: Any, flags: Dict[str, Any], rc: int):  # noqa: ARG002
        if rc == 0:
            logging.info("MQTT connected")
            client.publish(f"{self.base_topic}/status", "online", qos=1, retain=True)
        else:
            logging.warning("MQTT connection returned code %s", rc)

    def _on_disconnect(self, client: mqtt.Client, userdata: Any, rc: int):  # noqa: ARG002
        logging.warning("MQTT disconnected (rc=%s). Attempting reconnect.", rc)
        try:
            client.reconnect()
        except Exception as err:  # noqa: BLE001
            logging.error("MQTT reconnection failed: %s", err)

    def publish_heartbeat(self) -> None:
        timestamp = int(time.time())
        self.client.publish(f"{self.base_topic}/status", "online", qos=1, retain=True)
        self.client.publish(f"{self.base_topic}/last_seen", timestamp, qos=1, retain=True)

    def publish_device_state(self, device_id: str, payload: Dict[str, Any]) -> None:
        status_topic = f"{self.base_topic}/{device_id}/status"
        data_topic = f"{self.base_topic}/{device_id}/data"
        alarm_topic = f"{self.base_topic}/{device_id}/alarm"
        runtime_topic = f"{self.base_topic}/{device_id}/runtime"

        status_value = self._derive_status(payload)
        alarm_payload = payload.get("alarms") or payload.get("alarm_list") or []
        runtime_value = self._coalesce(payload, ["runtime", "run_hours", "runhour", "runtotal"], 0)

        self.client.publish(status_topic, status_value, qos=1, retain=True)
        self.client.publish(data_topic, json.dumps(payload), qos=1, retain=True)
        self.client.publish(alarm_topic, json.dumps(alarm_payload), qos=1, retain=True)
        self.client.publish(runtime_topic, runtime_value, qos=1, retain=True)

    @staticmethod
    def _derive_status(payload: Dict[str, Any]) -> str:
        if not isinstance(payload, dict):
            return "unknown"
        if payload.get("alarm") or payload.get("alarm_present") or SmartGenMqttBridge._coalesce(payload, ["alarm_count", "alarms"], 0):
            return "alarm"
        if SmartGenMqttBridge._to_bool(SmartGenMqttBridge._coalesce(payload, ["running", "run_state", "is_running", "Run"], False)):
            return "running"
        return str(payload.get("status") or payload.get("state") or "unknown")

    @staticmethod
    def _coalesce(data: Dict[str, Any], keys: List[str], default: Any) -> Any:
        for key in keys:
            if key in data and data[key] not in (None, ""):
                return data[key]
        return default

    @staticmethod
    def _to_bool(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value != 0
        if isinstance(value, str):
            return value.lower() in {"true", "1", "on", "yes"}
        return False


class SmartGenBridge:
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.poll_interval = int(config.get("poll_interval", DEFAULT_POLL_INTERVAL))
        self.api_client = SmartGenApiClient(config)
        self.mqtt_bridge = SmartGenMqttBridge(config)
        self.last_heartbeat = 0
        self.failure_count = 0

    def start(self) -> None:
        self.mqtt_bridge.connect()
        while True:
            self._tick()

    def _tick(self) -> None:
        self._maybe_heartbeat()
        try:
            snapshot = self.api_client.fetch_snapshot()
            if snapshot:
                device_id = str(snapshot.get("device_id") or self.api_client.device_id)
                self.mqtt_bridge.publish_device_state(device_id, snapshot)
                self.failure_count = 0
            else:
                self.failure_count += 1
                logging.warning("No snapshot available; failure count=%s", self.failure_count)
        except Exception as err:  # noqa: BLE001
            self.failure_count += 1
            logging.error("Error in main loop: %s", err)
        delay = self._backoff_delay()
        self._sleep_with_heartbeat(delay)

    def _maybe_heartbeat(self) -> None:
        now = time.time()
        if now - self.last_heartbeat >= HEARTBEAT_INTERVAL:
            self.mqtt_bridge.publish_heartbeat()
            self.last_heartbeat = now

    def _sleep_with_heartbeat(self, duration: float) -> None:
        end = time.time() + duration
        while time.time() < end:
            remaining = end - time.time()
            self._maybe_heartbeat()
            time.sleep(min(5, remaining))

    def _backoff_delay(self) -> int:
        if self.failure_count <= 0:
            return self.poll_interval
        steps = [5, 10, 30]
        idx = min(self.failure_count - 1, len(steps) - 1)
        return min(steps[idx], self.poll_interval)


def main() -> None:
    config = load_config()
    configure_logging(config.get("log_level", "info"))
    bridge = SmartGenBridge(config)
    bridge.start()


if __name__ == "__main__":
    main()
