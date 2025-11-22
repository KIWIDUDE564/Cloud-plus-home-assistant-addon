import hashlib
import json
import logging
import time
from typing import Any, Dict, Optional

import requests

REQUEST_TIMEOUT = 20
USER_AGENT = "okhttp/4.9.0"
SIGN_SECRET = "smartgen"


class SmartGenClient:
    """Client for SmartGen Cloud+/CloudGenMonDev endpoints."""

    def __init__(self, config: Dict[str, Any]):
        self.base_url = (config.get("api_base") or "https://www.smartgencloudplus.cn").rstrip("/")
        self.address = str(config.get("genset_address") or config.get("address") or "")
        self.language = config.get("language", "en-US")
        self.timezone = config.get("timezone", "Asia/Shanghai")
        self.token = config.get("token", "")
        self.utoken = config.get("utoken", "")
        self.cookie = config.get("cookie", "")
        self.sign_secret = config.get("sign_secret", SIGN_SECRET)
        self.session = requests.Session()
        self.device_id = self.address

    def _status_url(self) -> str:
        return f"{self.base_url}/yewu/devicedata/getstatus"

    def _build_signature(self, timestamp: str) -> str:
        """Compute request signature used for X-Sign header.

        The exact algorithm is derived from the web client's observed behavior: a
        simple MD5 over token, utoken, timestamp, and a shared secret.
        """

        payload = f"{self.token}{self.utoken}{timestamp}{self.sign_secret}".encode("utf-8")
        return hashlib.md5(payload).hexdigest()

    def _headers(self, timestamp: str) -> Dict[str, str]:
        headers: Dict[str, str] = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "X-Token": self.token,
            "X-Time": timestamp,
            "X-Sign": self._build_signature(timestamp),
        }
        if self.cookie:
            headers["Cookie"] = self.cookie
        return headers

    def _payload(self) -> Dict[str, Optional[str]]:
        return {
            "token": self.token,
            "utoken": self.utoken,
            "language": self.language,
            "timezone": self.timezone,
            "address": self.address,
        }

    @staticmethod
    def _parse_response(response: requests.Response) -> Dict[str, Any]:
        content_type = response.headers.get("Content-Type", "").lower()
        text_snippet = response.text[:500]
        logging.debug("SmartGen raw response (%s): %s", response.status_code, text_snippet)

        if "text/html" in content_type or text_snippet.strip().startswith("<"):
            logging.warning("Received HTML response from SmartGen; likely authentication failure")
            return {}

        try:
            return response.json()
        except json.JSONDecodeError:
            logging.warning("SmartGen response was not valid JSON")
            return {}

    def fetch_status(self) -> Dict[str, Any]:
        if not self.token or not self.utoken:
            raise ValueError("SmartGen token/utoken missing; cannot poll status")

        timestamp = str(int(time.time() * 1000))
        files = {k: (None, v) for k, v in self._payload().items() if v is not None}
        safe_headers = {k: ("***" if "token" in k.lower() else v) for k, v in self._headers(timestamp).items()}
        logging.debug("POST %s headers=%s", self._status_url(), safe_headers)

        try:
            response = self.session.post(
                self._status_url(),
                files=files,
                headers=self._headers(timestamp),
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as err:
            logging.error("SmartGen status request failed: %s", err)
            return {}

        data = self._parse_response(response)
        if isinstance(data, dict):
            self.token = data.get("token", self.token)
            self.utoken = data.get("utoken", self.utoken)
            payload = data.get("data") if isinstance(data.get("data"), dict) else data
            payload = payload or {}
            payload.setdefault("device_id", self.address)
            return payload
        return {}
