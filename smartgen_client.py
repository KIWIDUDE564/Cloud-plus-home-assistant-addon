import base64
import hashlib
import json
import logging
import time
from typing import Any, Dict, Optional

from Crypto.Cipher import AES
import requests

REQUEST_TIMEOUT = 20
USER_AGENT = "okhttp/4.9.0"
DEFAULT_SIGN_SECRET = "smartgen"
DEFAULT_AES_KEY = "smartgencloudplus"
DEFAULT_AES_IV = "smartgencloudplus"

LOGIN_PATH = "/yewu/user/login"
GENSET_LIST_PATH = "/yewu/genset/list"
STATUS_PATH = "/yewu/devicedata/getstatus"


def _pkcs7_pad(data: bytes, block_size: int = 16) -> bytes:
    padding = block_size - len(data) % block_size
    return data + bytes([padding] * padding)


def _pkcs7_unpad(data: bytes) -> bytes:
    if not data:
        return data
    padding = data[-1]
    if padding < 1 or padding > 16:
        return data
    return data[:-padding]


def _to_bytes(text: str, length: int = 16) -> bytes:
    raw = text.encode("utf-8")
    if len(raw) >= length:
        return raw[:length]
    return (raw + b"0" * length)[:length]


def _safe_json_loads(text: str) -> Dict[str, Any]:
    try:
        return json.loads(text)
    except Exception:  # noqa: BLE001
        logging.debug("Failed to decode JSON from decrypted payload: %s", text[:200])
        return {}


class SmartGenClient:
    """Client for SmartGen Cloud Plus endpoints mirrored from the web client."""

    def __init__(self, config: Dict[str, Any]):
        self.base_url = (config.get("api_base") or "http://www.smartgencloudplus.cn").rstrip("/")
        self.username = config.get("username") or ""
        self.password = config.get("password") or ""
        self.address = str(config.get("genset_address") or config.get("address") or "")
        self.language = config.get("language", "en-US")
        self.timezone = config.get("timezone", "Etc/UTC")
        self.token = config.get("token", "")
        self.utoken = config.get("utoken", "")
        self.cookie = config.get("cookie", "")
        self.sign_secret = (config.get("sign_secret") or DEFAULT_SIGN_SECRET)
        self.aes_key = _to_bytes(config.get("sign_secret") or DEFAULT_AES_KEY)
        self.aes_iv = _to_bytes(DEFAULT_AES_IV)
        self.session = requests.Session()
        self.device_id = self.address

    # --- crypto helpers -------------------------------------------------
    def _encrypt_param(self, payload: Dict[str, Any]) -> str:
        plaintext = json.dumps(payload, separators=(",", ":"))
        cipher = AES.new(self.aes_key, AES.MODE_CBC, iv=self.aes_iv)
        encrypted = cipher.encrypt(_pkcs7_pad(plaintext.encode("utf-8")))
        return base64.b64encode(encrypted).decode("utf-8")

    def _decrypt_param(self, encoded: str) -> Dict[str, Any]:
        try:
            cipher = AES.new(self.aes_key, AES.MODE_CBC, iv=self.aes_iv)
            decrypted = cipher.decrypt(base64.b64decode(encoded))
            unpadded = _pkcs7_unpad(decrypted).decode("utf-8", errors="ignore")
            return _safe_json_loads(unpadded)
        except Exception as err:  # noqa: BLE001
            logging.debug("Failed to decrypt payload: %s", err)
            return {}

    # --- request helpers ------------------------------------------------
    def _build_url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def _build_signature(self, timestamp: str) -> str:
        payload = f"{self.token}{self.utoken}{timestamp}{self.sign_secret}".encode("utf-8")
        return hashlib.md5(payload).hexdigest()

    def _headers(self, timestamp: Optional[str] = None) -> Dict[str, str]:
        headers: Dict[str, str] = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
        }
        if timestamp:
            headers.update(
                {
                    "X-Token": self.token,
                    "X-Time": timestamp,
                    "X-Sign": self._build_signature(timestamp),
                }
            )
        if self.cookie:
            headers["Cookie"] = self.cookie
        return headers

    def _post_json(self, path: str, data: Dict[str, Any], headers: Optional[Dict[str, str]] = None) -> requests.Response:
        url = self._build_url(path)
        merged_headers = {"Content-Type": "application/json", **(headers or {})}
        try:
            logging.debug("POST %s headers=%s", url, {k: ("***" if "token" in k.lower() else v) for k, v in merged_headers.items()})
            response = self.session.post(url, json=data, headers=merged_headers, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            return response
        except requests.RequestException as err:
            logging.error("SmartGen request failed for %s: %s", url, err)
            raise

    def _post_form(self, path: str, fields: Dict[str, Optional[str]], headers: Dict[str, str]) -> Optional[requests.Response]:
        url = self._build_url(path)
        files = {k: (None, v) for k, v in fields.items() if v is not None}
        safe_headers = {k: ("***" if "token" in k.lower() else v) for k, v in headers.items()}
        logging.debug("POST %s headers=%s", url, safe_headers)
        try:
            response = self.session.post(url, files=files, headers=headers, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            return response
        except requests.RequestException as err:
            logging.error("SmartGen request failed for %s: %s", url, err)
            return None

    @staticmethod
    def _parse_response(response: requests.Response) -> Dict[str, Any]:
        content_type = response.headers.get("Content-Type", "").lower()
        snippet = response.text[:500]
        logging.debug("SmartGen raw response (%s): %s", response.status_code, snippet)

        if "text/html" in content_type or snippet.strip().startswith("<"):
            logging.warning("Received HTML response from SmartGen; likely authentication failure")
            return {}
        try:
            return response.json()
        except json.JSONDecodeError:
            logging.warning("SmartGen response was not valid JSON")
            return {}

    # --- public operations ----------------------------------------------
    def login(self) -> None:
        if not self.username or not self.password:
            logging.debug("No credentials provided; skipping login")
            return

        payload = {"account": self.username, "password": self.password}
        encrypted = self._encrypt_param(payload)
        body = {"paramStr": encrypted}
        response = self._post_json(LOGIN_PATH, body, headers=self._headers())
        data = self._parse_response(response)
        if not data:
            raise RuntimeError("SmartGen login failed: empty response")

        token_info: Dict[str, Any] = {}
        if isinstance(data.get("data"), dict):
            token_info = data["data"]
        elif isinstance(data.get("data"), str):
            token_info = self._decrypt_param(data.get("data", ""))
        if not token_info:
            token_info = data

        self.token = token_info.get("token", self.token)
        self.utoken = token_info.get("utoken", self.utoken)
        if not self.token or not self.utoken:
            raise RuntimeError("SmartGen login did not return token/utoken")
        logging.info("SmartGen login successful")

    def ensure_logged_in(self) -> None:
        if self.token and self.utoken:
            return
        self.login()

    def fetch_genset_list(self) -> Dict[str, Any]:
        self.ensure_logged_in()
        timestamp = str(int(time.time() * 1000))
        response = self._post_form(
            GENSET_LIST_PATH,
            {
                "token": self.token,
                "utoken": self.utoken,
                "language": self.language,
                "timezone": self.timezone,
            },
            headers=self._headers(timestamp),
        )
        if not response:
            return {}
        data = self._parse_response(response)
        if isinstance(data.get("data"), str):
            data = self._decrypt_param(data.get("data", ""))
        return data if isinstance(data, dict) else {}

    def fetch_status(self) -> Dict[str, Any]:
        self.ensure_logged_in()
        if not self.address:
            raise ValueError("SmartGen genset_address is required")

        timestamp = str(int(time.time() * 1000))
        response = self._post_form(
            STATUS_PATH,
            {
                "token": self.token,
                "utoken": self.utoken,
                "language": self.language,
                "timezone": self.timezone,
                "address": self.address,
            },
            headers=self._headers(timestamp),
        )
        if not response:
            return {}

        data = self._parse_response(response)
        payload: Dict[str, Any] = {}
        if isinstance(data.get("data"), str):
            payload = self._decrypt_param(data.get("data", ""))
        elif isinstance(data.get("data"), dict):
            payload = data.get("data", {})
        elif isinstance(data, dict):
            payload = data

        payload = payload or {}
        payload.setdefault("device_id", self.address)
        return payload
