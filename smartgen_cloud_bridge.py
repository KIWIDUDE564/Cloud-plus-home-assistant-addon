import json
import logging
import os
import time
from typing import Any, Dict, Optional

import paho.mqtt.client as mqtt
import requests

OPTIONS_PATH = "/data/options.json"
DEFAULT_POLL_INTERVAL = 60
DEFAULT_BASE_TOPIC = "smartgen"
DEFAULT_LANGUAGE = "en-US"
DEFAULT_TIMEZONE = "Asia/Shanghai"
DEFAULT_REQUEST_TIMEOUT = 15


class SmartGenCloudBridge:
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.api_base = (config.get("api_base") or "http://www.smartgencloudplus.cn").rstrip("/")
        self.token = config.get("token", "")
        self.utoken = config.get("utoken", "")
        self.language = config.get("language", DEFAULT_LANGUAGE)
        self.timezone = config.get("timezone", DEFAULT_TIMEZONE)
        self.genset_address = str(config.get("genset_address", ""))
        self.poll_interval = int(config.get("poll_interval", DEFAULT_POLL_INTERVAL))
        self.request_timeout = int(config.get("request_timeout", DEFAULT_REQUEST_TIMEOUT))
        self.base_topic = config.get("base_topic", DEFAULT_BASE_TOPIC)
        self.mqtt_host = config.get("mqtt_host", "core-mosquitto")
        self.mqtt_port = int(config.get("mqtt_port", 1883))
        self.mqtt_username = config.get("mqtt_username") or None
        self.mqtt_password = config.get("mqtt_password") or None
        self.session = requests.Session()
        self.mqtt_client = self._setup_mqtt()

    def _setup_mqtt(self) -> mqtt.Client:
        client = mqtt.Client()
        if self.mqtt_username:
            client.username_pw_set(self.mqtt_username, self.mqtt_password)
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect

        while True:
            try:
                logging.info("Connecting to MQTT %s:%s", self.mqtt_host, self.mqtt_port)
                client.connect(self.mqtt_host, self.mqtt_port, keepalive=60)
                client.loop_start()
                break
            except Exception as err:  # noqa: BLE001
                logging.error("MQTT connection failed: %s", err)
                time.sleep(5)
        return client

    @staticmethod
    def _on_connect(client: mqtt.Client, userdata: Any, flags: Dict[str, Any], rc: int):  # noqa: ARG002
        if rc == 0:
            logging.info("MQTT connected")
        else:
            logging.warning("MQTT connection returned code %s", rc)

    @staticmethod
    def _on_disconnect(client: mqtt.Client, userdata: Any, rc: int):  # noqa: ARG002
        logging.warning("MQTT disconnected (rc=%s)", rc)

    def _headers(self) -> Dict[str, str]:
        headers = {
            "User-Agent": "okhttp/4.9.0",
            "Accept": "application/json, text/plain, */*",
        }
        if self.token:
            headers["Cookie"] = f"smartgenyun_web={self.token}"
        return headers

    def _request(self, url: str, *, data: Optional[Dict[str, Any]] = None, json_body: Optional[Dict[str, Any]] = None) -> Optional[requests.Response]:
        try:
            if json_body is not None:
                response = self.session.post(url, json=json_body, headers=self._headers(), timeout=self.request_timeout)
            else:
                response = self.session.post(url, data=data, headers=self._headers(), timeout=self.request_timeout)
            response.raise_for_status()
            return response
        except requests.RequestException as err:
            logging.error("Request failed for %s: %s", url, err)
            return None

    def fetch_status(self) -> Optional[Dict[str, Any]]:
        url = f"{self.api_base}/yewu/devicedata/getstatus"
        payload = {
            "add": self.genset_address,
            "token": self.token,
            "utoken": self.utoken,
            "language": self.language,
            "timezone": self.timezone,
        }
        response = self._request(url, data=payload)
        if not response:
            return None
        try:
            return response.json()
        except json.JSONDecodeError:
            logging.error("Failed to decode status response")
            return None

    def fetch_monitor_list(self) -> Optional[Dict[str, Any]]:
        url = f"{self.api_base}/yewu/realTimeData/monitorList"
        payload = {
            "token": self.token,
            "utoken": self.utoken,
            "language": self.language,
            "timezone": self.timezone,
        }
        response = self._request(url, json_body=payload)
        if not response:
            return None
        try:
            return response.json()
        except json.JSONDecodeError:
            logging.error("Failed to decode monitor list response")
            return None

    def publish_status(self, data: Dict[str, Any]) -> None:
        topic = f"{self.base_topic}/{self.genset_address}/status/raw"
        self.mqtt_client.publish(topic, json.dumps(data), qos=1, retain=True)

    def publish_monitor_list(self, data: Dict[str, Any]) -> None:
        if not isinstance(data, dict):
            return
        groups = data.get("data") or []
        now = int(time.time())
        for group in groups:
            items = group.get("list") or []
            for item in items:
                itemadd = item.get("itemadd") or item.get("add") or "unknown"
                name = item.get("itemname") or itemadd
                payload = {
                    "name": name,
                    "itemadd": itemadd,
                    "val": item.get("val"),
                    "unit": item.get("unit"),
                    "special": item.get("special"),
                }
                topic = f"{self.base_topic}/{self.genset_address}/{itemadd}"
                self.mqtt_client.publish(topic, json.dumps(payload), qos=1, retain=True)
        self.mqtt_client.publish(f"{self.base_topic}/{self.genset_address}/last_update", str(now), qos=1, retain=True)

    def run(self) -> None:
        while True:
            try:
                status = self.fetch_status()
                if status is not None:
                    self.publish_status(status)
                monitor = self.fetch_monitor_list()
                if monitor is not None:
                    self.publish_monitor_list(monitor)
            except Exception as err:  # noqa: BLE001
                logging.error("Unexpected error in main loop: %s", err)
            time.sleep(self.poll_interval)


def load_config(path: str = OPTIONS_PATH) -> Dict[str, Any]:
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as file:
                return json.load(file)
        except Exception as err:  # noqa: BLE001
            logging.error("Failed to load config: %s", err)
    return {}


def configure_logging(level: str) -> None:
    logging.basicConfig(level=getattr(logging, level.upper(), logging.INFO), format="%(asctime)s [%(levelname)s] %(message)s")


def main() -> None:
    config = load_config()
    configure_logging(config.get("log_level", "info"))
    bridge = SmartGenCloudBridge(config)
    bridge.run()


if __name__ == "__main__":
    main()
